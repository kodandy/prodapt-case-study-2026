"""gRPC CreatePayment carries the optional `settlement_currency` (B2.3 over B8.2) and maps each 422 outcome
of `charge_card` to its own status: idempotency_key_reused and fx_amount_too_small → INVALID_ARGUMENT with a
field violation, fx_rate_unavailable → FAILED_PRECONDITION, any other 422 → fixed INTERNAL.

Drives a real in-process grpc server on 127.0.0.1:0 through the generated stub. The root fixture truncates
every gateway table, so each rig puts back the 12 migration-seeded demo rates (`seed_demo_rates`).
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import grpc
import pytest
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.merchant_api import grpc_server
from gateway.merchant_api.grpc_server import GrpcDeps, build_grpc_server
from gateway.merchant_api.pb import payments_pb2, payments_pb2_grpc
from gateway.payments.charge import ChargeDeps, ChargeOutcome
from gateway.payments.fx.service import seed_demo_rates
from gateway.payments.providers.simulator.provider import SimulatorProvider
from google.rpc import error_details_pb2  # type: ignore[import-untyped]  # no py.typed marker
from grpc_status import rpc_status
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
RPC_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class Rig:
    stub: payments_pb2_grpc.MerchantPaymentsStub
    simulator: SimulatorProvider
    session_factory: sessionmaker[Session]
    api_key: str


def _rig(session_factory: sessionmaker[Session], clock: FixedClock) -> Iterator[Rig]:
    with session_factory.begin() as session:
        seed_demo_rates(session)
    simulator = SimulatorProvider(session_factory, clock)
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=simulator,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        merchant = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    server, port = build_grpc_server(GrpcDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER), "127.0.0.1:0")
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield Rig(
            payments_pb2_grpc.MerchantPaymentsStub(channel), simulator, session_factory, merchant.api_key
        )
    finally:
        channel.close()
        server.stop(None)


@pytest.fixture
def rig(session_factory: sessionmaker[Session], clock: FixedClock) -> Iterator[Rig]:
    yield from _rig(session_factory, clock)


@pytest.fixture
def rig_before_rates(session_factory: sessionmaker[Session]) -> Iterator[Rig]:
    """The seeded rates are valid from 2026-01-01."""
    yield from _rig(session_factory, FixedClock(datetime(2025, 12, 31, 12, 0, tzinfo=UTC)))


def _request(
    amount_minor: int = 1_501, currency: str = "USD", settlement_currency: str | None = None
) -> payments_pb2.CreatePaymentRequest:
    request = payments_pb2.CreatePaymentRequest(amount_minor=amount_minor, currency=currency, method="card")
    request.card.CopyFrom(payments_pb2.CardInput(number=APPROVE_PAN, exp_month=12, exp_year=2030, cvc="123"))
    if settlement_currency is not None:
        request.settlement_currency = settlement_currency
    return request


def _create(
    rig: Rig, request: payments_pb2.CreatePaymentRequest, key: str | None = None
) -> payments_pb2.CreatePaymentResponse:
    metadata = (("authorization", f"Bearer {rig.api_key}"), ("idempotency-key", key or uuid.uuid4().hex))
    response: payments_pb2.CreatePaymentResponse = rig.stub.CreatePayment(
        request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS
    )
    return response


def _failure(rig: Rig, request: payments_pb2.CreatePaymentRequest, key: str | None = None) -> grpc.RpcError:
    with pytest.raises(grpc.RpcError) as caught:
        _create(rig, request, key)
    return caught.value


def _violations(error: grpc.RpcError) -> list[tuple[str, str]]:
    assert isinstance(error, grpc.Call)
    status = rpc_status.from_call(error)
    assert status is not None
    bad_request = error_details_pb2.BadRequest()
    assert status.details[0].Unpack(bad_request)
    return [(violation.field, violation.description) for violation in bad_request.field_violations]


def _code(error: grpc.RpcError) -> grpc.StatusCode:
    assert isinstance(error, grpc.Call)
    code: grpc.StatusCode = error.code()
    return code


def _details(error: grpc.RpcError) -> str:
    assert isinstance(error, grpc.Call)
    details: str = error.details()
    return details


def _merchant_payable(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT p.currency, p.amount_minor, j.fx_rate_id FROM gateway.postings p "
                "JOIN gateway.journal_entries j ON j.id = p.journal_id "
                "JOIN gateway.ledger_accounts a ON a.id = p.account_id WHERE a.kind = 'merchant_payable'"
            )
        ).mappings()
        return [dict(row) for row in rows]


def _payment_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.payments")).scalar_one())


# --- settlement_currency ------------------------------------------------------------------------------


def test_create_payment_without_a_settlement_currency_settles_in_the_card_currency(rig: Rig) -> None:
    # Arrange
    request = _request()

    # Act
    response = _create(rig, request)

    # Assert
    assert (response.state, response.amount_minor, response.currency) == ("succeeded", 1_501, "USD")
    assert _merchant_payable(rig.session_factory) == [
        {"currency": "USD", "amount_minor": -1_501, "fx_rate_id": None}
    ]


def test_create_payment_with_a_settlement_currency_settles_the_merchant_in_that_currency(rig: Rig) -> None:
    # Arrange: 1501 USD minor at the demo USD/EUR rate floors to 1380 EUR minor (test_fx_charge.py)
    request = _request(settlement_currency="EUR")

    # Act
    response = _create(rig, request)

    # Assert
    assert (response.state, response.amount_minor, response.currency) == ("succeeded", 1_501, "USD")
    assert _merchant_payable(rig.session_factory) == [
        {"currency": "EUR", "amount_minor": -1_380, "fx_rate_id": "demo-USDEUR-v1"}
    ]


@pytest.mark.parametrize("settlement_currency", ["GBP", "usd", ""])
def test_create_payment_with_an_unsupported_settlement_currency_is_invalid_argument(
    rig: Rig, settlement_currency: str
) -> None:
    # Arrange
    request = _request(settlement_currency=settlement_currency)

    # Act
    error = _failure(rig, request)

    # Assert
    assert _code(error) == grpc.StatusCode.INVALID_ARGUMENT
    assert [field for field, _ in _violations(error)] == ["settlement_currency"]
    assert _payment_count(rig.session_factory) == 0
    assert rig.simulator.charge_count() == 0


# --- 422 mapping --------------------------------------------------------------------------------------


def test_a_reused_idempotency_key_stays_invalid_argument_on_the_idempotency_key(rig: Rig) -> None:
    # Arrange
    _create(rig, _request(), key="k-reuse")

    # Act
    error = _failure(rig, _request(amount_minor=1_600), key="k-reuse")

    # Assert
    assert _code(error) == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(error) == [("idempotency_key", "idempotency_key_reused")]


def test_a_converted_amount_of_zero_is_invalid_argument_and_never_reaches_the_provider(rig: Rig) -> None:
    # Arrange: 1 JPY converts to 0 USD minor units
    request = _request(amount_minor=1, currency="JPY", settlement_currency="USD")

    # Act
    error = _failure(rig, request)

    # Assert
    assert _code(error) == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(error) == [("amount_minor", "fx_amount_too_small")]
    assert _payment_count(rig.session_factory) == 0
    assert rig.simulator.charge_count() == 0


def test_no_rate_in_force_is_failed_precondition_and_never_reaches_the_provider(
    rig_before_rates: Rig,
) -> None:
    # Arrange
    request = _request(settlement_currency="EUR")

    # Act
    error = _failure(rig_before_rates, request)

    # Assert
    assert (_code(error), _details(error)) == (grpc.StatusCode.FAILED_PRECONDITION, "fx_rate_unavailable")
    assert _payment_count(rig_before_rates.session_factory) == 0
    assert rig_before_rates.simulator.charge_count() == 0


def test_an_unknown_422_error_code_is_a_fixed_internal_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    def unknown_422(*_args: object, **_kwargs: object) -> ChargeOutcome:
        return ChargeOutcome(422, {"error": "some_future_error"})

    monkeypatch.setattr(grpc_server, "charge_card", unknown_422)

    # Act
    error = _failure(rig, _request())

    # Assert
    assert (_code(error), _details(error)) == (grpc.StatusCode.INTERNAL, "internal error")
