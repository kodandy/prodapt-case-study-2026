"""RED tests for the merchant gRPC server (B8.2 / B8.4), written from the gw-grpc-stripe contract section 2.

They drive a real in-process grpc server on 127.0.0.1:0 through the generated stub, against the same
Postgres-backed domain functions and the same REST app the HTTP transport uses.
"""

import threading
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import grpc
import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.merchant_api.grpc_server import MAX_MESSAGE_BYTES, GrpcDeps, build_grpc_server
from gateway.merchant_api.pb import payments_pb2, payments_pb2_grpc
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.port import CardRef, ProviderResult, ProviderTimeoutError
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from google.rpc import error_details_pb2  # type: ignore[import-untyped]  # no py.typed marker
from grpc_status import rpc_status
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
MASTERCARD_PAN = "5555555555554444"
INSUFFICIENT_FUNDS_PAN = "4000000000009995"
DECLINED_PAN = "4000000000000002"
ISSUER_UNAVAILABLE_PAN = "4000000000000119"
NON_TEST_LUHN_PAN = "4111111111111111"
UNKNOWN_OUTCOME_LAST4 = "0119"
DEFAULT_PAGE_SIZE = 20
RPC_TIMEOUT_SECONDS = 20.0
THREAD_WAIT_SECONDS = 10.0
IDEMPOTENCY_WAIT_SECONDS = 0.3
MISSING_API_KEY = "missing api key"
INVALID_API_KEY = "invalid api key"
PAYMENT_NOT_FOUND = "payment not found"
IN_PROGRESS = "idempotency_in_progress"
KEY_REUSED = "idempotency_key_reused"
EXPECTED_MAX_MESSAGE_BYTES = 64 * 1024


class RecordingProvider:
    """Delegates to the simulator; records charges; can time out for one card or block until released."""

    def __init__(self, inner: SimulatorProvider) -> None:
        self.name = inner.name
        self._inner = inner
        self.charges: list[str] = []
        self.timeout_last4: str | None = None
        self.block_until_released = False
        self.entered = threading.Event()
        self.release = threading.Event()

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        self.charges.append(provider_key)
        self.entered.set()
        if self.block_until_released:
            self.release.wait(THREAD_WAIT_SECONDS)
        if card.last4 == self.timeout_last4:
            raise ProviderTimeoutError
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclass(frozen=True)
class Rig:
    charge: ChargeDeps
    provider: RecordingProvider
    merchant_id: uuid.UUID
    api_key: str
    other_merchant_id: uuid.UUID
    other_api_key: str
    rest: TestClient
    stub: payments_pb2_grpc.MerchantPaymentsStub
    clock: FixedClock
    session_factory: sessionmaker[Session] = field(repr=False)


@pytest.fixture
def rig(session_factory: sessionmaker[Session], clock: FixedClock) -> Iterator[Rig]:
    provider = RecordingProvider(SimulatorProvider(session_factory, clock))
    charge_deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=provider,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
        idempotency_wait_seconds=IDEMPOTENCY_WAIT_SECONDS,
    )
    with session_factory() as session:
        merchant = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        other = create_merchant(session, clock, "Globex", TEST_API_KEY_PEPPER)
        session.commit()
    server, port = build_grpc_server(
        GrpcDeps(charge=charge_deps, api_key_pepper=TEST_API_KEY_PEPPER), "127.0.0.1:0"
    )
    server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    rest = TestClient(create_app(GatewayAppDeps(charge=charge_deps, api_key_pepper=TEST_API_KEY_PEPPER)))
    try:
        yield Rig(
            charge=charge_deps,
            provider=provider,
            merchant_id=merchant.merchant_id,
            api_key=merchant.api_key,
            other_merchant_id=other.merchant_id,
            other_api_key=other.api_key,
            rest=rest,
            stub=payments_pb2_grpc.MerchantPaymentsStub(channel),
            clock=clock,
            session_factory=session_factory,
        )
    finally:
        provider.release.set()
        channel.close()
        server.stop(None)


def _meta(api_key: str, idempotency_key: str | None = None) -> tuple[tuple[str, str], ...]:
    pairs = [("authorization", f"Bearer {api_key}")]
    if idempotency_key is not None:
        pairs.append(("idempotency-key", idempotency_key))
    return tuple(pairs)


def _create_request(
    *,
    amount_minor: int = 1500,
    currency: str = "USD",
    method: str = "card",
    pan: str = APPROVE_PAN,
    exp_month: int = 12,
    exp_year: int = 2030,
    cvc: str = "123",
    with_card: bool = True,
) -> payments_pb2.CreatePaymentRequest:
    request = payments_pb2.CreatePaymentRequest(amount_minor=amount_minor, currency=currency, method=method)
    if with_card:
        request.card.CopyFrom(
            payments_pb2.CardInput(number=pan, exp_month=exp_month, exp_year=exp_year, cvc=cvc)
        )
    return request


def _rest_body(request: payments_pb2.CreatePaymentRequest) -> dict[str, object]:
    body: dict[str, object] = {"amount_minor": request.amount_minor, "currency": request.currency}
    if request.method:
        body["method"] = request.method
    if request.HasField("card"):
        body["card"] = {
            "number": request.card.number,
            "exp_month": request.card.exp_month,
            "exp_year": request.card.exp_year,
            "cvc": request.card.cvc,
        }
    return body


def _json_object(response_json: object) -> dict[str, object]:
    assert isinstance(response_json, dict)
    return response_json


def _rest_create(rig: Rig, request: payments_pb2.CreatePaymentRequest, key: str) -> dict[str, object]:
    response = rig.rest.post(
        "/v1/payments",
        json=_rest_body(request),
        headers={"Authorization": f"Bearer {rig.api_key}", "Idempotency-Key": key},
    )
    assert response.status_code == 201
    return _json_object(response.json())


def _rest_get(rig: Rig, payment_id: str, api_key: str | None = None) -> dict[str, object]:
    response = rig.rest.get(
        f"/v1/payments/{payment_id}", headers={"Authorization": f"Bearer {api_key or rig.api_key}"}
    )
    assert response.status_code == 200
    return _json_object(response.json())


def _rest_list(rig: Rig, params: Mapping[str, str | int]) -> dict[str, object]:
    response = rig.rest.get(
        "/v1/payments", params=dict(params), headers={"Authorization": f"Bearer {rig.api_key}"}
    )
    assert response.status_code == 200
    return _json_object(response.json())


def _charge(rig: Rig, merchant_id: uuid.UUID, pan: str = APPROVE_PAN, amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(rig.charge, merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _charge_series(rig: Rig, merchant_id: uuid.UUID, count: int) -> list[tuple[uuid.UUID, datetime]]:
    """`count` payments one minute apart, oldest first, with their creation instants."""
    created: list[tuple[uuid.UUID, datetime]] = []
    for index in range(count):
        created.append((_charge(rig, merchant_id, amount_minor=1000 + index), rig.clock()))
        rig.clock.advance(timedelta(minutes=1))
    return created


def _set_totals(rig: Rig, payment_id: uuid.UUID, refunded: int, disputed: int, chargeback: int) -> None:
    with rig.session_factory() as session:
        session.execute(
            text(
                "UPDATE gateway.payments SET refunded_minor = :r, disputed_minor = :d, chargeback_minor = :c "
                "WHERE id = :id"
            ),
            {"r": refunded, "d": disputed, "c": chargeback, "id": payment_id},
        )
        session.commit()


def _clear_card_token(rig: Rig, payment_id: uuid.UUID) -> None:
    with rig.session_factory() as session:
        session.execute(
            text("UPDATE gateway.payments SET card_token = NULL WHERE id = :id"), {"id": payment_id}
        )
        session.commit()


def _call_of(info: pytest.ExceptionInfo[grpc.RpcError]) -> grpc.Call:
    error = info.value
    assert isinstance(error, grpc.Call)
    return error


def _violations(call: grpc.Call) -> list[tuple[str, str]]:
    status = rpc_status.from_call(call)
    assert status is not None
    assert len(status.details) == 1
    bad_request = error_details_pb2.BadRequest()
    assert status.details[0].Unpack(bad_request)
    return [(violation.field, violation.description) for violation in bad_request.field_violations]


def _rest_violations(rig: Rig, request: payments_pb2.CreatePaymentRequest) -> list[tuple[str, str]]:
    response = rig.rest.post(
        "/v1/payments",
        json=_rest_body(request),
        headers={"Authorization": f"Bearer {rig.api_key}", "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 422
    violations = _json_object(response.json())["violations"]
    assert isinstance(violations, list)
    return [(str(item["field"]), str(item["description"])) for item in violations]


def _assert_matches_rest(message: payments_pb2.Payment, rest: Mapping[str, object]) -> None:
    assert message.payment_id == rest["payment_id"]
    assert message.state == rest["state"]
    assert message.amount_minor == rest["amount_minor"]
    assert message.currency == rest["currency"]
    assert message.method == rest["method"]
    assert message.refunded_minor == rest["refunded_minor"]
    assert message.disputed_minor == rest["disputed_minor"]
    assert message.chargeback_minor == rest["chargeback_minor"]
    assert message.created_at == rest["created_at"]
    rest_card = rest["card"]
    if rest_card is None:
        assert not message.HasField("card")
    else:
        assert isinstance(rest_card, dict)
        assert message.HasField("card")
        assert (message.card.bin, message.card.last4) == (rest_card["bin"], rest_card["last4"])
    rest_decline = rest["decline_code"]
    if rest_decline is None:
        assert not message.HasField("decline_code")
    else:
        assert message.HasField("decline_code")
        assert message.decline_code == rest_decline


# --- server construction -------------------------------------------------------------------------------


def test_max_message_bytes_is_64_kib() -> None:
    # Arrange / Act
    limit = MAX_MESSAGE_BYTES

    # Assert
    assert limit == EXPECTED_MAX_MESSAGE_BYTES


def test_grpc_deps_repr_hides_the_api_key_pepper(rig: Rig) -> None:
    # Arrange
    deps = GrpcDeps(charge=rig.charge, api_key_pepper=TEST_API_KEY_PEPPER)

    # Act
    rendered = repr(deps)

    # Assert
    assert TEST_API_KEY_PEPPER not in rendered


def test_build_grpc_server_binds_an_ephemeral_port_without_starting(rig: Rig) -> None:
    # Arrange
    deps = GrpcDeps(charge=rig.charge, api_key_pepper=TEST_API_KEY_PEPPER)

    # Act
    server, port = build_grpc_server(deps, "127.0.0.1:0")
    try:
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        with pytest.raises(grpc.FutureTimeoutError):
            grpc.channel_ready_future(channel).result(timeout=0.5)
        channel.close()
    finally:
        server.stop(None)

    # Assert
    assert port > 0


# --- authentication ------------------------------------------------------------------------------------


def test_every_rpc_without_authorization_metadata_is_unauthenticated(rig: Rig) -> None:
    # Arrange
    calls: list[Callable[[], object]] = [
        lambda: rig.stub.CreatePayment(
            _create_request(), metadata=(("idempotency-key", uuid.uuid4().hex),), timeout=RPC_TIMEOUT_SECONDS
        ),
        lambda: rig.stub.GetPayment(
            payments_pb2.GetPaymentRequest(payment_id=str(uuid.uuid4())), timeout=RPC_TIMEOUT_SECONDS
        ),
        lambda: rig.stub.ListPayments(payments_pb2.ListPaymentsRequest(), timeout=RPC_TIMEOUT_SECONDS),
    ]
    outcomes: list[tuple[grpc.StatusCode, str]] = []

    # Act
    for call in calls:
        with pytest.raises(grpc.RpcError) as info:
            call()
        failed = _call_of(info)
        outcomes.append((failed.code(), failed.details() or ""))

    # Assert
    assert outcomes == [(grpc.StatusCode.UNAUTHENTICATED, MISSING_API_KEY)] * 3


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", "Bearer", "sk_test_no_scheme_at_all"])
def test_a_malformed_authorization_value_is_unauthenticated(rig: Rig, authorization: str) -> None:
    # Arrange
    metadata = (("authorization", authorization),)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.ListPayments(
            payments_pb2.ListPaymentsRequest(), metadata=metadata, timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    assert _call_of(info).code() == grpc.StatusCode.UNAUTHENTICATED


def test_an_unknown_api_key_is_unauthenticated_with_invalid_api_key(rig: Rig) -> None:
    # Arrange
    unknown_key = rig.api_key[:-4] + ("0000" if not rig.api_key.endswith("0000") else "1111")

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.ListPayments(
            payments_pb2.ListPaymentsRequest(), metadata=_meta(unknown_key), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    failed = _call_of(info)
    assert (failed.code(), failed.details()) == (grpc.StatusCode.UNAUTHENTICATED, INVALID_API_KEY)


def test_authentication_is_checked_before_request_validation_and_before_charging(rig: Rig) -> None:
    # Arrange
    invalid_request = _create_request(amount_minor=0, currency="GBP")

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(invalid_request, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert (failed.code(), failed.details()) == (grpc.StatusCode.UNAUTHENTICATED, MISSING_API_KEY)
    assert rig.provider.charges == []


# --- CreatePayment: outcomes ---------------------------------------------------------------------------


def test_create_payment_succeeds_with_the_stored_body(rig: Rig) -> None:
    # Arrange
    request = _create_request()

    # Act
    response = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    assert uuid.UUID(response.payment_id).version == 4
    assert response.state == "succeeded"
    assert response.amount_minor == 1500
    assert response.currency == "USD"
    assert response.method == "card"
    assert (response.card.bin, response.card.last4) == ("424242", "4242")
    assert not response.HasField("decline_code")


@pytest.mark.parametrize(
    ("pan", "decline_code"),
    [(DECLINED_PAN, "card_declined"), (INSUFFICIENT_FUNDS_PAN, "insufficient_funds")],
)
def test_create_payment_returns_declined_with_the_decline_code(rig: Rig, pan: str, decline_code: str) -> None:
    # Arrange
    request = _create_request(pan=pan)

    # Act
    response = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    assert response.state == "declined"
    assert response.HasField("decline_code")
    assert response.decline_code == decline_code


def test_create_payment_maps_a_provider_timeout_to_ok_with_state_unknown(rig: Rig) -> None:
    # Arrange
    rig.provider.timeout_last4 = UNKNOWN_OUTCOME_LAST4
    request = _create_request(pan=ISSUER_UNAVAILABLE_PAN)

    # Act
    response = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    assert response.state == "unknown"
    assert (response.card.bin, response.card.last4) == ("400000", UNKNOWN_OUTCOME_LAST4)
    assert not response.HasField("decline_code")


def test_create_payment_with_an_empty_method_uses_the_card_default(rig: Rig) -> None:
    # Arrange
    request = _create_request(method="")

    # Act
    response = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    assert response.method == "card"
    assert response.state == "succeeded"


def test_create_payment_creates_a_payment_the_rest_api_can_read(rig: Rig) -> None:
    # Arrange
    request = _create_request(pan=MASTERCARD_PAN, amount_minor=2750)

    # Act
    created = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    rest = _rest_get(rig, created.payment_id)
    assert rest["state"] == "succeeded"
    assert rest["amount_minor"] == 2750
    assert rest["card"] == {"bin": "555555", "last4": "4444"}


# --- CreatePayment: idempotency (shared store with REST) -----------------------------------------------


def test_the_same_key_and_body_replays_the_stored_response_with_one_provider_charge(rig: Rig) -> None:
    # Arrange
    request = _create_request()
    metadata = _meta(rig.api_key, "replay-key-1")

    # Act
    first = rig.stub.CreatePayment(request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)
    second = rig.stub.CreatePayment(request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    assert second == first
    assert len(rig.provider.charges) == 1


def test_a_replayed_unknown_outcome_returns_the_same_payment_without_calling_the_provider_again(
    rig: Rig,
) -> None:
    # Arrange
    rig.provider.timeout_last4 = UNKNOWN_OUTCOME_LAST4
    request = _create_request(pan=ISSUER_UNAVAILABLE_PAN)
    metadata = _meta(rig.api_key, "replay-unknown-1")

    # Act
    first = rig.stub.CreatePayment(request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)
    second = rig.stub.CreatePayment(request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    assert (first.state, second.state) == ("unknown", "unknown")
    assert second.payment_id == first.payment_id
    assert len(rig.provider.charges) == 1


def test_a_rest_charge_then_a_grpc_charge_with_the_same_key_and_body_return_the_same_payment(
    rig: Rig,
) -> None:
    # Arrange
    request = _create_request()
    rest_body = _rest_create(rig, request, "shared-key-rest-first")

    # Act
    grpc_response = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, "shared-key-rest-first"), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    assert grpc_response.payment_id == rest_body["payment_id"]
    assert grpc_response.state == rest_body["state"]
    assert len(rig.provider.charges) == 1


def test_a_grpc_charge_then_a_rest_charge_with_the_same_key_and_body_return_the_same_payment(
    rig: Rig,
) -> None:
    # Arrange
    request = _create_request()
    grpc_response = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, "shared-key-grpc-first"), timeout=RPC_TIMEOUT_SECONDS
    )

    # Act
    rest_body = _rest_create(rig, request, "shared-key-grpc-first")

    # Assert
    assert rest_body["payment_id"] == grpc_response.payment_id
    assert len(rig.provider.charges) == 1


def test_the_same_key_with_a_different_body_is_invalid_argument_with_a_key_reused_violation(rig: Rig) -> None:
    # Arrange
    metadata = _meta(rig.api_key, "reused-key-1")
    rig.stub.CreatePayment(_create_request(amount_minor=1500), metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(
            _create_request(amount_minor=1501), metadata=metadata, timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(failed) == [("idempotency_key", KEY_REUSED)]
    assert len(rig.provider.charges) == 1


def test_the_same_key_in_flight_is_aborted_with_idempotency_in_progress(rig: Rig) -> None:
    # Arrange
    rig.provider.block_until_released = True
    request = _create_request()
    metadata = _meta(rig.api_key, "in-flight-key-1")
    first = rig.stub.CreatePayment.future(request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)
    assert rig.provider.entered.wait(THREAD_WAIT_SECONDS)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(request, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)
    rig.provider.release.set()
    first_response = first.result(timeout=THREAD_WAIT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert (failed.code(), failed.details()) == (grpc.StatusCode.ABORTED, IN_PROGRESS)
    assert first_response.state == "succeeded"
    assert len(rig.provider.charges) == 1


def test_the_same_idempotency_key_is_scoped_per_merchant(rig: Rig) -> None:
    # Arrange
    request = _create_request()

    # Act
    mine = rig.stub.CreatePayment(
        request, metadata=_meta(rig.api_key, "tenant-key-1"), timeout=RPC_TIMEOUT_SECONDS
    )
    theirs = rig.stub.CreatePayment(
        request, metadata=_meta(rig.other_api_key, "tenant-key-1"), timeout=RPC_TIMEOUT_SECONDS
    )

    # Assert
    assert mine.payment_id != theirs.payment_id
    assert len(rig.provider.charges) == 2


# --- CreatePayment: idempotency-key metadata -----------------------------------------------------------


def test_create_payment_without_an_idempotency_key_is_invalid_argument(rig: Rig) -> None:
    # Arrange
    metadata = _meta(rig.api_key)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(_create_request(), metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(failed) == [("idempotency_key", "idempotency-key metadata is required")]
    assert rig.provider.charges == []


@pytest.mark.parametrize("key", ["has a space", "k" * 256], ids=["space", "256_chars"])
def test_create_payment_with_an_invalid_idempotency_key_is_invalid_argument(rig: Rig, key: str) -> None:
    # Arrange
    metadata = (("authorization", f"Bearer {rig.api_key}"), ("idempotency-key", key))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(_create_request(), metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(failed) == [("idempotency_key", "invalid idempotency-key metadata")]
    assert rig.provider.charges == []


def test_create_payment_accepts_a_255_character_idempotency_key(rig: Rig) -> None:
    # Arrange
    metadata = _meta(rig.api_key, "k" * 255)

    # Act
    response = rig.stub.CreatePayment(_create_request(), metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    assert response.state == "succeeded"


# --- CreatePayment: validation -------------------------------------------------------------------------

INVALID_REQUESTS = [
    pytest.param(_create_request(amount_minor=0), ["amount_minor"], id="amount_zero"),
    pytest.param(_create_request(amount_minor=-5), ["amount_minor"], id="amount_negative"),
    pytest.param(_create_request(currency="GBP"), ["currency"], id="currency_unsupported"),
    pytest.param(_create_request(currency=""), ["currency"], id="currency_empty"),
    pytest.param(_create_request(method="wallet"), ["method"], id="method_unsupported"),
    pytest.param(_create_request(pan=NON_TEST_LUHN_PAN), ["card.number"], id="pan_not_a_test_card"),
    pytest.param(_create_request(cvc="12345"), ["card.cvc"], id="cvc_too_long"),
    pytest.param(_create_request(cvc="12a"), ["card.cvc"], id="cvc_not_digits"),
    pytest.param(_create_request(exp_month=13), ["card.exp_month"], id="exp_month_13"),
    pytest.param(_create_request(exp_year=1999), ["card.exp_year"], id="exp_year_1999"),
    pytest.param(_create_request(with_card=False), ["card"], id="card_unset"),
]


@pytest.mark.parametrize(("request_message", "fields"), INVALID_REQUESTS)
def test_create_payment_with_an_invalid_request_is_invalid_argument_with_field_violations(
    rig: Rig, request_message: payments_pb2.CreatePaymentRequest, fields: list[str]
) -> None:
    # Arrange
    metadata = _meta(rig.api_key, uuid.uuid4().hex)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(request_message, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert [field_name for field_name, _description in _violations(failed)] == fields
    assert rig.provider.charges == []


@pytest.mark.parametrize(("request_message", "fields"), INVALID_REQUESTS)
def test_grpc_field_violations_equal_the_rest_violations(
    rig: Rig, request_message: payments_pb2.CreatePaymentRequest, fields: list[str]
) -> None:
    # Arrange
    expected = _rest_violations(rig, request_message)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(
            request_message, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    assert [field_name for field_name, _description in expected] == fields
    assert _violations(_call_of(info)) == expected


def test_create_payment_reports_every_violation_of_a_request_with_several_problems(rig: Rig) -> None:
    # Arrange
    request = _create_request(amount_minor=0, currency="GBP", cvc="1")

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(
            request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    fields = [field_name for field_name, _description in _violations(_call_of(info))]
    assert sorted(fields) == ["amount_minor", "card.cvc", "currency"]


@pytest.mark.parametrize("pan", [NON_TEST_LUHN_PAN, "4242424242424241", "4" * 40])
def test_a_rejected_pan_and_cvc_never_appear_in_the_status(rig: Rig, pan: str) -> None:
    # Arrange
    cvc = "98765"
    request = _create_request(pan=pan, cvc=cvc)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(
            request, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    failed = _call_of(info)
    status = rpc_status.from_call(failed)
    assert status is not None
    serialized = status.SerializeToString()
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert pan not in (failed.details() or "")
    assert cvc not in (failed.details() or "")
    assert pan.encode() not in serialized
    assert cvc.encode() not in serialized


# --- GetPayment ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("pan", [APPROVE_PAN, DECLINED_PAN, INSUFFICIENT_FUNDS_PAN])
def test_get_payment_equals_the_rest_detail(rig: Rig, pan: str) -> None:
    # Arrange
    payment_id = _charge(rig, rig.merchant_id, pan=pan)

    # Act
    message = rig.stub.GetPayment(
        payments_pb2.GetPaymentRequest(payment_id=str(payment_id)),
        metadata=_meta(rig.api_key),
        timeout=RPC_TIMEOUT_SECONDS,
    )

    # Assert
    _assert_matches_rest(message, _rest_get(rig, str(payment_id)))
    assert message.HasField("decline_code") is (pan != APPROVE_PAN)


def test_get_payment_carries_the_refund_dispute_and_chargeback_totals(rig: Rig) -> None:
    # Arrange
    payment_id = _charge(rig, rig.merchant_id)
    _set_totals(rig, payment_id, refunded=100, disputed=200, chargeback=300)

    # Act
    message = rig.stub.GetPayment(
        payments_pb2.GetPaymentRequest(payment_id=str(payment_id)),
        metadata=_meta(rig.api_key),
        timeout=RPC_TIMEOUT_SECONDS,
    )

    # Assert
    assert (message.refunded_minor, message.disputed_minor, message.chargeback_minor) == (100, 200, 300)
    _assert_matches_rest(message, _rest_get(rig, str(payment_id)))


def test_get_payment_leaves_card_unset_when_the_rest_card_is_null(rig: Rig) -> None:
    # Arrange
    payment_id = _charge(rig, rig.merchant_id)
    _clear_card_token(rig, payment_id)

    # Act
    message = rig.stub.GetPayment(
        payments_pb2.GetPaymentRequest(payment_id=str(payment_id)),
        metadata=_meta(rig.api_key),
        timeout=RPC_TIMEOUT_SECONDS,
    )

    # Assert
    assert _rest_get(rig, str(payment_id))["card"] is None
    assert not message.HasField("card")


def test_get_payment_created_at_is_the_rest_json_string(rig: Rig) -> None:
    # Arrange
    payment_id = _charge(rig, rig.merchant_id)

    # Act
    message = rig.stub.GetPayment(
        payments_pb2.GetPaymentRequest(payment_id=str(payment_id)),
        metadata=_meta(rig.api_key),
        timeout=RPC_TIMEOUT_SECONDS,
    )

    # Assert
    assert message.created_at == _rest_get(rig, str(payment_id))["created_at"]
    assert datetime.fromisoformat(message.created_at) == rig.clock()


@pytest.mark.parametrize("payment_id", ["not-a-uuid", "", "12345678-1234-1234-1234-12345678901"])
def test_get_payment_with_a_malformed_id_is_invalid_argument(rig: Rig, payment_id: str) -> None:
    # Arrange
    request = payments_pb2.GetPaymentRequest(payment_id=payment_id)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.GetPayment(request, metadata=_meta(rig.api_key), timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(failed) == [("payment_id", "invalid payment_id")]


def test_get_payment_unknown_and_foreign_ids_are_the_same_not_found(rig: Rig) -> None:
    # Arrange
    foreign_id = _charge(rig, rig.other_merchant_id)
    outcomes: list[tuple[grpc.StatusCode, str | None]] = []

    # Act
    for payment_id in (uuid.uuid4(), foreign_id):
        with pytest.raises(grpc.RpcError) as info:
            rig.stub.GetPayment(
                payments_pb2.GetPaymentRequest(payment_id=str(payment_id)),
                metadata=_meta(rig.api_key),
                timeout=RPC_TIMEOUT_SECONDS,
            )
        failed = _call_of(info)
        outcomes.append((failed.code(), failed.details()))

    # Assert
    assert outcomes == [(grpc.StatusCode.NOT_FOUND, PAYMENT_NOT_FOUND)] * 2


# --- ListPayments --------------------------------------------------------------------------------------


def _grpc_list(rig: Rig, request: payments_pb2.ListPaymentsRequest) -> payments_pb2.ListPaymentsResponse:
    return rig.stub.ListPayments(request, metadata=_meta(rig.api_key), timeout=RPC_TIMEOUT_SECONDS)


def test_list_payments_returns_the_callers_payments_newest_first(rig: Rig) -> None:
    # Arrange
    created = _charge_series(rig, rig.merchant_id, 3)
    _charge_series(rig, rig.other_merchant_id, 2)

    # Act
    response = _grpc_list(rig, payments_pb2.ListPaymentsRequest())

    # Assert
    expected_ids = [str(payment_id) for payment_id, _ in reversed(created)]
    assert [item.payment_id for item in response.items] == expected_ids
    assert not response.HasField("next_cursor")


def test_list_payments_of_a_merchant_without_payments_is_empty(rig: Rig) -> None:
    # Arrange
    _charge_series(rig, rig.other_merchant_id, 2)

    # Act
    response = _grpc_list(rig, payments_pb2.ListPaymentsRequest())

    # Assert
    assert list(response.items) == []
    assert not response.HasField("next_cursor")


def test_list_payments_limit_zero_means_the_default_page_size(rig: Rig) -> None:
    # Arrange
    _charge_series(rig, rig.merchant_id, DEFAULT_PAGE_SIZE + 2)

    # Act
    response = _grpc_list(rig, payments_pb2.ListPaymentsRequest(limit=0))

    # Assert
    assert len(response.items) == DEFAULT_PAGE_SIZE
    assert response.HasField("next_cursor")


def test_list_payments_items_equal_the_rest_items(rig: Rig) -> None:
    # Arrange
    created = _charge_series(rig, rig.merchant_id, 3)
    _charge(rig, rig.merchant_id, pan=DECLINED_PAN)
    _set_totals(rig, created[0][0], refunded=10, disputed=20, chargeback=30)
    rest_items = _rest_list(rig, {})["items"]
    assert isinstance(rest_items, list)

    # Act
    response = _grpc_list(rig, payments_pb2.ListPaymentsRequest())

    # Assert
    assert len(response.items) == len(rest_items) == 4
    for message, rest_item in zip(response.items, rest_items, strict=True):
        _assert_matches_rest(message, _json_object(rest_item))


def test_list_payments_pages_and_cursors_equal_the_rest_pages(rig: Rig) -> None:
    # Arrange
    _charge_series(rig, rig.merchant_id, 5)
    grpc_pages: list[tuple[list[str], str | None]] = []
    rest_pages: list[tuple[list[str], str | None]] = []

    # Act
    grpc_cursor: str | None = None
    for _ in range(5):
        request = payments_pb2.ListPaymentsRequest(limit=2)
        if grpc_cursor is not None:
            request.cursor = grpc_cursor
        response = _grpc_list(rig, request)
        grpc_cursor = response.next_cursor if response.HasField("next_cursor") else None
        grpc_pages.append(([item.payment_id for item in response.items], grpc_cursor))
        if grpc_cursor is None:
            break
    rest_cursor: str | None = None
    for _ in range(5):
        params: dict[str, str | int] = {"limit": 2}
        if rest_cursor is not None:
            params["cursor"] = rest_cursor
        body = _rest_list(rig, params)
        rest_items = body["items"]
        assert isinstance(rest_items, list)
        next_cursor = body["next_cursor"]
        assert next_cursor is None or isinstance(next_cursor, str)
        rest_cursor = next_cursor
        rest_pages.append(([str(_json_object(item)["payment_id"]) for item in rest_items], rest_cursor))
        if rest_cursor is None:
            break

    # Assert
    assert [len(ids) for ids, _ in grpc_pages] == [2, 2, 1]
    assert grpc_pages == rest_pages


def test_list_payments_state_and_method_filters_equal_rest(rig: Rig) -> None:
    # Arrange
    _charge(rig, rig.merchant_id)
    declined_id = _charge(rig, rig.merchant_id, pan=DECLINED_PAN)
    _charge(rig, rig.merchant_id, pan=INSUFFICIENT_FUNDS_PAN)

    # Act
    declined = _grpc_list(rig, payments_pb2.ListPaymentsRequest(state="declined", method="card"))
    succeeded = _grpc_list(rig, payments_pb2.ListPaymentsRequest(state="succeeded"))

    # Assert
    rest_declined = _rest_list(rig, {"state": "declined", "method": "card"})["items"]
    assert isinstance(rest_declined, list)
    assert [item.payment_id for item in declined.items] == [
        str(_json_object(item)["payment_id"]) for item in rest_declined
    ]
    assert str(declined_id) in {item.payment_id for item in declined.items}
    assert len(declined.items) == 2
    assert [item.state for item in succeeded.items] == ["succeeded"]


def test_list_payments_date_range_is_half_open_and_equals_rest(rig: Rig) -> None:
    # Arrange
    created = _charge_series(rig, rig.merchant_id, 4)
    created_from = created[1][1]
    created_to = created[3][1]

    # Act
    response = _grpc_list(
        rig,
        payments_pb2.ListPaymentsRequest(
            created_from=created_from.isoformat(), created_to=created_to.isoformat()
        ),
    )

    # Assert
    rest_items = _rest_list(
        rig, {"created_from": created_from.isoformat(), "created_to": created_to.isoformat()}
    )["items"]
    assert isinstance(rest_items, list)
    assert [item.payment_id for item in response.items] == [str(created[2][0]), str(created[1][0])]
    assert [item.payment_id for item in response.items] == [
        str(_json_object(item)["payment_id"]) for item in rest_items
    ]


def test_list_payments_with_a_cursor_resumes_after_it(rig: Rig) -> None:
    # Arrange
    created = _charge_series(rig, rig.merchant_id, 3)
    first_page = _grpc_list(rig, payments_pb2.ListPaymentsRequest(limit=1))

    # Act
    second_page = _grpc_list(rig, payments_pb2.ListPaymentsRequest(limit=2, cursor=first_page.next_cursor))

    # Assert
    assert [item.payment_id for item in first_page.items] == [str(created[2][0])]
    assert [item.payment_id for item in second_page.items] == [str(created[1][0]), str(created[0][0])]


def test_list_payments_accepts_the_maximum_limit_of_100(rig: Rig) -> None:
    # Arrange
    _charge_series(rig, rig.merchant_id, 2)

    # Act
    response = _grpc_list(rig, payments_pb2.ListPaymentsRequest(limit=100))

    # Assert
    assert len(response.items) == 2


LIST_VIOLATIONS = [
    pytest.param(
        payments_pb2.ListPaymentsRequest(created_from="2026-11-01T00:00:00"),
        [("created_from", "invalid created_from")],
        id="created_from_naive",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(created_from="2026-11-01"),
        [("created_from", "invalid created_from")],
        id="created_from_date_only",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(created_from="9999-12-31T23:59:59-23:59"),
        [("created_from", "invalid created_from")],
        id="created_from_unrepresentable_in_utc",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(created_to="nope"),
        [("created_to", "invalid created_to")],
        id="created_to_garbage",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(
            created_from="2026-11-02T00:00:00Z", created_to="2026-11-01T00:00:00Z"
        ),
        [("created_to", "invalid date range")],
        id="range_reversed",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(
            created_from="2026-11-01T00:00:00Z", created_to="2026-11-01T00:00:00Z"
        ),
        [("created_to", "invalid date range")],
        id="range_empty",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(method="wallet"), [("method", "invalid method")], id="method"
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(method="CARD"), [("method", "invalid method")], id="method_case"
    ),
    pytest.param(payments_pb2.ListPaymentsRequest(state="bogus"), [("state", "invalid state")], id="state"),
    pytest.param(payments_pb2.ListPaymentsRequest(limit=101), [("limit", "invalid limit")], id="limit_101"),
    pytest.param(
        payments_pb2.ListPaymentsRequest(limit=-1), [("limit", "invalid limit")], id="limit_negative"
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(cursor="c" * 513),
        [("cursor", "invalid cursor")],
        id="cursor_too_long",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(cursor="not-a-cursor"),
        [("cursor", "invalid cursor")],
        id="cursor_undecodable",
    ),
    pytest.param(
        payments_pb2.ListPaymentsRequest(created_from="2026-11-01T00:00:00", method="wallet", limit=500),
        [("created_from", "invalid created_from"), ("method", "invalid method"), ("limit", "invalid limit")],
        id="several_in_contract_order",
    ),
]


@pytest.mark.parametrize(("request_message", "expected"), LIST_VIOLATIONS)
def test_list_payments_with_invalid_filters_is_invalid_argument_with_the_shared_violations(
    rig: Rig, request_message: payments_pb2.ListPaymentsRequest, expected: list[tuple[str, str]]
) -> None:
    # Arrange
    metadata = _meta(rig.api_key)

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.ListPayments(request_message, metadata=metadata, timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert _violations(failed) == expected


# --- message size --------------------------------------------------------------------------------------


def test_a_message_over_the_size_limit_is_resource_exhausted(rig: Rig) -> None:
    # Arrange
    oversized = _create_request(currency="x" * (MAX_MESSAGE_BYTES + 1024))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(
            oversized, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    assert _call_of(info).code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    assert rig.provider.charges == []


def test_a_message_under_the_size_limit_reaches_validation(rig: Rig) -> None:
    # Arrange
    large = _create_request(currency="x" * (MAX_MESSAGE_BYTES // 2))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        rig.stub.CreatePayment(
            large, metadata=_meta(rig.api_key, uuid.uuid4().hex), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    failed = _call_of(info)
    assert failed.code() == grpc.StatusCode.INVALID_ARGUMENT
    assert [field_name for field_name, _description in _violations(failed)] == ["currency"]
