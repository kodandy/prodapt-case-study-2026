"""RED tests: a provider that refuses the request (not a card decline) leaves the charge outcome unknown.

The payment must be finalized as `unknown` (never left in `created`), answered 202, and replayed from the
stored idempotent response without calling the provider again.
"""

import dataclasses
import uuid

import pytest
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.port import CardRef, ProviderRequestError, ProviderResult
from gateway.payments.providers.stripe.provider import StripeRequestError
from gateway.payments.schemas import CardSummary, ChargeRequest, ChargeResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
IDEMPOTENCY_KEY = "key-request-error"


class RefusingProvider:
    """Provider whose `charge` always refuses the request with the given error; counts calls."""

    def __init__(self, error: Exception) -> None:
        self.name = "acq_refusing"
        self.calls = 0
        self._error = error

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        self.calls += 1
        raise self._error

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return None


@dataclasses.dataclass(frozen=True)
class Rig:
    deps: ChargeDeps
    provider: RefusingProvider
    merchant_id: uuid.UUID


def _rig(session_factory: sessionmaker[Session], clock: FixedClock, error: Exception) -> Rig:
    merchant_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Merchant(id=merchant_id, name="Acme", created_at=clock()))
        session.commit()
    provider = RefusingProvider(error)
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=provider,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return Rig(deps, provider, merchant_id)


def _request() -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _expected_unknown_body(payment_id: object) -> dict[str, object]:
    return ChargeResponse(
        payment_id=uuid.UUID(str(payment_id)),
        state="unknown",
        amount_minor=1500,
        currency="USD",
        method="card",
        card=CardSummary(bin="424242", last4="4242"),
        decline_code=None,
    ).model_dump(mode="json")


REQUEST_ERRORS = [
    pytest.param(ProviderRequestError("refused"), id="provider_request_error"),
    pytest.param(StripeRequestError("stripe rejected the request with HTTP 400"), id="stripe_request_error"),
]


@pytest.mark.parametrize("error", REQUEST_ERRORS)
def test_provider_request_error_is_an_unknown_outcome_returned_as_202(
    session_factory: sessionmaker[Session], clock: FixedClock, error: Exception
) -> None:
    # Arrange
    rig = _rig(session_factory, clock, error)

    # Act
    outcome = charge_card(rig.deps, rig.merchant_id, IDEMPOTENCY_KEY, _request())

    # Assert
    assert outcome.status_code == 202
    assert outcome.body == _expected_unknown_body(outcome.body["payment_id"])
    assert outcome.body["state"] == "unknown"
    assert outcome.body["decline_code"] is None
    assert rig.provider.calls == 1


@pytest.mark.parametrize("error", REQUEST_ERRORS)
def test_provider_request_error_finalizes_the_payment_and_attempt_as_unknown_not_created(
    session_factory: sessionmaker[Session], clock: FixedClock, error: Exception
) -> None:
    # Arrange
    rig = _rig(session_factory, clock, error)

    # Act
    outcome = charge_card(rig.deps, rig.merchant_id, IDEMPOTENCY_KEY, _request())

    # Assert
    assert _rows(session_factory, "SELECT id, state, decline_code FROM gateway.payments") == [
        {"id": uuid.UUID(str(outcome.body["payment_id"])), "state": "unknown", "decline_code": None}
    ]
    assert _rows(session_factory, "SELECT state, provider_ref FROM gateway.payment_attempts") == [
        {"state": "unknown", "provider_ref": None}
    ]


@pytest.mark.parametrize("error", REQUEST_ERRORS)
def test_provider_request_error_stores_the_202_response_on_the_idempotency_row(
    session_factory: sessionmaker[Session], clock: FixedClock, error: Exception
) -> None:
    # Arrange
    rig = _rig(session_factory, clock, error)

    # Act
    outcome = charge_card(rig.deps, rig.merchant_id, IDEMPOTENCY_KEY, _request())

    # Assert
    assert _rows(
        session_factory,
        "SELECT key, payment_id, response_status, response_body FROM gateway.idempotency_keys",
    ) == [
        {
            "key": IDEMPOTENCY_KEY,
            "payment_id": uuid.UUID(str(outcome.body["payment_id"])),
            "response_status": 202,
            "response_body": outcome.body,
        }
    ]


@pytest.mark.parametrize("error", REQUEST_ERRORS)
def test_replay_after_provider_request_error_returns_the_stored_202_without_calling_the_provider(
    session_factory: sessionmaker[Session], clock: FixedClock, error: Exception
) -> None:
    # Arrange
    rig = _rig(session_factory, clock, error)
    request = _request()
    first = charge_card(rig.deps, rig.merchant_id, IDEMPOTENCY_KEY, request)

    # Act
    replay = charge_card(rig.deps, rig.merchant_id, IDEMPOTENCY_KEY, request)

    # Assert
    assert first.status_code == 202
    assert replay.status_code == 202
    assert replay.body == first.body
    assert rig.provider.calls == 1
    assert _rows(session_factory, "SELECT state FROM gateway.payments") == [{"state": "unknown"}]


@pytest.mark.parametrize("error", REQUEST_ERRORS)
def test_provider_request_error_writes_no_journal_outbox_or_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock, error: Exception
) -> None:
    # Arrange
    rig = _rig(session_factory, clock, error)

    # Act
    charge_card(rig.deps, rig.merchant_id, IDEMPOTENCY_KEY, _request())

    # Assert
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries") == [{"n": 0}]
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.outbox") == [{"n": 0}]
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.audit_log") == [{"n": 0}]
