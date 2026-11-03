"""RED tests for B7.1 charge-path wiring, written from docs/specs/gateway-basic.md.

The score is stored on the payment (tx1) and read through `get_payment_risk` / `GET /v1/payments/{id}/risk`;
`ChargeResponse` and the B3 history are unchanged. A blocked charge is settled as `declined` / `risk_blocked`
in tx1 without ever reaching the provider.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, ChargeOutcome, charge_card
from gateway.payments.disputes import PaymentNotFoundError
from gateway.payments.history import list_payments
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.risk import get_payment_risk
from gateway.payments.schemas import (
    CardSummary,
    ChargeRequest,
    ChargeResponse,
    PaymentRiskView,
    PaymentSummary,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
HIGH_USD = 500_000
HISTORY_ITEM_KEYS = {
    "payment_id",
    "state",
    "amount_minor",
    "currency",
    "method",
    "card",
    "decline_code",
    "refunded_minor",
    "disputed_minor",
    "chargeback_minor",
    "created_at",
}
CHARGE_RESPONSE_KEYS = {"payment_id", "state", "amount_minor", "currency", "method", "card", "decline_code"}


class CountingProvider:
    """Wraps a provider and counts `charge` calls (thread-safe)."""

    def __init__(self, inner: ProviderPort) -> None:
        self.name = inner.name
        self._inner = inner
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    provider: CountingProvider
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    auth: dict[str, str]


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    shared: Harness | None = None,
    name: str = "Acme",
) -> Harness:
    """A merchant with an API key; passing `shared` adds a second merchant on the same deps and provider."""
    if shared is None:
        provider = CountingProvider(SimulatorProvider(session_factory, clock))
        deps = ChargeDeps(
            session_factory=session_factory,
            redis=None,
            provider=provider,
            clock=clock,
            card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
        )
    else:
        provider, deps = shared.provider, shared.deps
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(
        deps,
        provider,
        session_factory,
        clock,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
    )


def _client(h: Harness) -> TestClient:
    return TestClient(create_app(GatewayAppDeps(charge=h.deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _body(pan: str, amount_minor: int, currency: str = "USD") -> dict[str, object]:
    return {
        "amount_minor": amount_minor,
        "currency": currency,
        "method": "card",
        "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }


def _request(pan: str = APPROVE_PAN, amount_minor: int = 1500, currency: str = "USD") -> ChargeRequest:
    return ChargeRequest.model_validate(_body(pan, amount_minor, currency))


def _charge(
    h: Harness,
    pan: str = APPROVE_PAN,
    amount_minor: int = 1500,
    currency: str = "USD",
    key: str | None = None,
) -> ChargeOutcome:
    return charge_card(h.deps, h.merchant_id, key or uuid.uuid4().hex, _request(pan, amount_minor, currency))


def _payment_id(outcome: ChargeOutcome) -> uuid.UUID:
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _prime(h: Harness, pan: str, count: int) -> list[uuid.UUID]:
    """`count` small charges one minute apart; the clock ends `count` minutes after it started."""
    ids: list[uuid.UUID] = []
    for _ in range(count):
        ids.append(_payment_id(_charge(h, pan=pan)))
        h.clock.advance(timedelta(minutes=1))
    return ids


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str) -> int:
    with session_factory() as session:
        return int(session.execute(text(query)).scalar_one())


def _risk_row(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> dict[str, object]:
    rows = _rows(
        session_factory,
        "SELECT state, decline_code, risk_score, risk_reasons, risk_flagged "
        "FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )
    assert len(rows) == 1
    return rows[0]


def _expected_charge_body(
    outcome: ChargeOutcome, state: str, bin_: str, last4: str, decline_code: str | None, amount: int
) -> dict[str, object]:
    return ChargeResponse(
        payment_id=uuid.UUID(str(outcome.body["payment_id"])),
        state=state,
        amount_minor=amount,
        currency="USD",
        method="card",
        card=CardSummary(bin=bin_, last4=last4),
        decline_code=decline_code,
    ).model_dump(mode="json")


# --- stored assessment ---------------------------------------------------------------------------------


def test_clean_charge_stores_score_zero_no_reasons_and_not_flagged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _charge(h)

    # Assert
    row = _risk_row(session_factory, _payment_id(outcome))
    assert row["state"] == "succeeded"
    assert row["risk_score"] == Decimal("0.00")
    assert str(row["risk_score"]) == "0.00"
    assert row["risk_reasons"] == []
    assert row["risk_flagged"] is False


def test_high_amount_alone_is_stored_with_its_reason_and_is_not_flagged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _charge(h, amount_minor=HIGH_USD)

    # Assert
    row = _risk_row(session_factory, _payment_id(outcome))
    assert row["state"] == "succeeded"
    assert str(row["risk_score"]) == "0.40"
    assert row["risk_reasons"] == ["high_amount"]
    assert row["risk_flagged"] is False
    assert h.provider.calls == 1


def test_a_charge_does_not_count_itself_so_the_first_three_charges_stay_clean_and_the_fourth_hits_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _prime(h, APPROVE_PAN, 4)

    # Act
    rows = [_risk_row(session_factory, payment_id) for payment_id in ids]

    # Assert
    assert [str(row["risk_score"]) for row in rows] == ["0.00", "0.00", "0.00", "0.40"]
    assert [row["risk_reasons"] for row in rows] == [[], [], [], ["card_velocity"]]
    assert [row["risk_flagged"] for row in rows] == [False, False, False, False]


def test_velocity_ages_out_once_the_earlier_payments_leave_the_ten_minute_window(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, APPROVE_PAN, 3)
    clock.advance(timedelta(minutes=11))

    # Act
    outcome = _charge(h)

    # Assert
    row = _risk_row(session_factory, _payment_id(outcome))
    assert str(row["risk_score"]) == "0.00"
    assert row["risk_reasons"] == []


def test_velocity_counts_the_same_card_used_at_another_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _harness(session_factory, clock)
    second = _harness(session_factory, clock, shared=first, name="Other")
    _prime(first, APPROVE_PAN, 3)

    # Act
    outcome = _charge(second, amount_minor=HIGH_USD)

    # Assert
    row = _risk_row(session_factory, _payment_id(outcome))
    assert str(row["risk_score"]) == "0.80"
    assert row["risk_reasons"] == ["high_amount", "card_velocity"]
    assert row["risk_flagged"] is True


def test_velocity_ignores_a_different_card(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, DECLINED_PAN, 3)

    # Act
    outcome = _charge(h, pan=APPROVE_PAN)

    # Assert
    row = _risk_row(session_factory, _payment_id(outcome))
    assert str(row["risk_score"]) == "0.00"
    assert row["risk_reasons"] == []


# --- flagged but not blocked ---------------------------------------------------------------------------


def test_flagged_but_not_blocked_charge_still_calls_the_provider_and_succeeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, APPROVE_PAN, 3)
    calls_before = h.provider.calls

    # Act
    outcome = _charge(h, amount_minor=HIGH_USD)

    # Assert
    payment_id = _payment_id(outcome)
    assert outcome.body == _expected_charge_body(outcome, "succeeded", "424242", "4242", None, HIGH_USD)
    assert h.provider.calls == calls_before + 1
    row = _risk_row(session_factory, payment_id)
    assert row["state"] == "succeeded"
    assert row["decline_code"] is None
    assert row["risk_score"] == Decimal("0.80")
    assert str(row["risk_score"]) == "0.80"
    assert row["risk_reasons"] == ["high_amount", "card_velocity"]
    assert row["risk_flagged"] is True


def test_flagged_but_not_blocked_charge_posts_its_journal(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, APPROVE_PAN, 3)

    # Act
    outcome = _charge(h, amount_minor=HIGH_USD)

    # Assert
    payment_id = _payment_id(outcome)
    journals = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = 'payment' AND ref_id = :id",
        id=payment_id,
    )
    assert journals == [{"n": 1}]


def test_the_provider_decline_of_a_flagged_charge_keeps_the_provider_code_not_risk_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: three earlier declines on the card, small amount => velocity 0.40 + declines 0.30 = 0.70
    h = _harness(session_factory, clock)
    _prime(h, DECLINED_PAN, 3)
    calls_before = h.provider.calls

    # Act
    outcome = _charge(h, pan=DECLINED_PAN)

    # Assert
    payment_id = _payment_id(outcome)
    assert outcome.body == _expected_charge_body(outcome, "declined", "400000", "0002", "card_declined", 1500)
    assert h.provider.calls == calls_before + 1
    row = _risk_row(session_factory, payment_id)
    assert str(row["risk_score"]) == "0.70"
    assert row["risk_reasons"] == ["card_velocity", "recent_declines"]
    assert row["risk_flagged"] is True


# --- blocked -------------------------------------------------------------------------------------------


def _blocked_charge(h: Harness, key: str = "key-blocked") -> ChargeOutcome:
    """Three small declines make velocity + recent_declines hit; a high amount adds the third rule."""
    _prime(h, DECLINED_PAN, 3)
    return _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD, key=key)


def test_blocked_charge_never_calls_the_provider(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, DECLINED_PAN, 3)
    calls_before = h.provider.calls
    simulator_charges_before = _count(session_factory, "SELECT count(*) FROM simulator.charges")

    # Act
    outcome = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD)

    # Assert
    assert outcome.status_code == 201
    assert (calls_before, h.provider.calls) == (3, 3)
    assert _count(session_factory, "SELECT count(*) FROM simulator.charges") == simulator_charges_before


def test_blocked_charge_returns_201_declined_with_decline_code_risk_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    assert outcome.status_code == 201
    assert set(outcome.body) == CHARGE_RESPONSE_KEYS
    assert outcome.body == _expected_charge_body(
        outcome, "declined", "400000", "0002", "risk_blocked", HIGH_USD
    )


def test_blocked_charge_stores_the_capped_score_every_reason_in_order_and_the_flag(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    row = _risk_row(session_factory, _payment_id(outcome))
    assert row["state"] == "declined"
    assert row["decline_code"] == "risk_blocked"
    assert row["risk_score"] == Decimal("1.00")
    assert str(row["risk_score"]) == "1.00"
    assert row["risk_reasons"] == ["high_amount", "card_velocity", "recent_declines"]
    assert row["risk_flagged"] is True


def test_blocked_charge_posts_no_journal(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    payment_id = _payment_id(outcome)
    own = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = 'payment' AND ref_id = :id",
        id=payment_id,
    )
    assert own == [{"n": 0}]
    assert _count(session_factory, "SELECT count(*) FROM gateway.journal_entries") == 0


def test_blocked_charge_writes_exactly_one_payment_declined_outbox_row_with_risk_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    payment_id = _payment_id(outcome)
    rows = _rows(
        session_factory,
        "SELECT topic, payload FROM gateway.outbox WHERE payload->>'payment_id' = :pid",
        pid=str(payment_id),
    )
    assert len(rows) == 1
    assert rows[0]["topic"] == PAYMENTS_TOPIC
    event = PaymentEventV1.model_validate(rows[0]["payload"])
    assert event.event_type == "payment.declined"
    assert event.state == "declined"
    assert event.decline_code == "risk_blocked"
    assert (event.amount_minor, event.currency) == (HIGH_USD, "USD")
    assert (event.card_bin, event.card_last4) == ("400000", "0002")
    assert event.merchant_id == h.merchant_id
    assert event.provider is None


def test_blocked_charge_writes_one_audit_row_for_the_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    payment_id = _payment_id(outcome)
    rows = _rows(
        session_factory,
        "SELECT action, actor_id, summary->>'state' AS state, (summary->>'amount_minor')::bigint AS amount "
        "FROM gateway.audit_log WHERE target = :target",
        target=f"payment:{payment_id}",
    )
    assert rows == [
        {
            "action": "payment.charge",
            "actor_id": f"merchant:{h.merchant_id}",
            "state": "declined",
            "amount": HIGH_USD,
        }
    ]


def test_blocked_charge_replays_the_stored_body_for_the_same_idempotency_key_without_a_provider_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    first = _blocked_charge(h, key="key-replay")
    payments_before = _count(session_factory, "SELECT count(*) FROM gateway.payments")
    calls_before = h.provider.calls
    outbox_before = _count(session_factory, "SELECT count(*) FROM gateway.outbox")

    # Act
    replay = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD, key="key-replay")

    # Assert
    assert replay == first
    assert replay.status_code == 201
    assert h.provider.calls == calls_before
    assert _count(session_factory, "SELECT count(*) FROM gateway.payments") == payments_before
    assert _count(session_factory, "SELECT count(*) FROM gateway.outbox") == outbox_before


def test_blocked_charge_stores_its_201_body_on_the_idempotency_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h, key="key-stored")

    # Assert
    records = _rows(
        session_factory,
        "SELECT payment_id, response_status, response_body FROM gateway.idempotency_keys WHERE key = :key",
        key="key-stored",
    )
    assert records == [
        {
            "payment_id": _payment_id(outcome),
            "response_status": 201,
            "response_body": outcome.body,
        }
    ]


def test_blocked_payments_do_not_count_as_declines_for_the_cards_next_charge(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: two blocks on the card, then the clock moves until only the blocks are inside the 60-minute
    # decline window (the three provider declines have aged out) and nothing is inside the velocity window
    h = _harness(session_factory, clock)
    first_block = _blocked_charge(h)
    clock.advance(timedelta(minutes=1))
    second_block = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD)
    clock.advance(timedelta(minutes=59))
    calls_before = h.provider.calls

    # Act
    outcome = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD)

    # Assert
    assert first_block.body["decline_code"] == "risk_blocked"
    assert second_block.body["decline_code"] == "risk_blocked"
    row = _risk_row(session_factory, _payment_id(outcome))
    assert row["state"] == "declined"
    assert row["decline_code"] == "card_declined"
    assert str(row["risk_score"]) == "0.40"
    assert row["risk_reasons"] == ["high_amount"]
    assert row["risk_flagged"] is False
    assert h.provider.calls == calls_before + 1


# --- ChargeResponse unchanged ---------------------------------------------------------------------------


def test_post_payments_response_body_carries_no_risk_fields_for_a_flagged_charge(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, APPROVE_PAN, 3)
    client = _client(h)

    # Act
    response = client.post(
        "/v1/payments", json=_body(APPROVE_PAN, HIGH_USD), headers={**h.auth, "Idempotency-Key": "key-http"}
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert set(body) == CHARGE_RESPONSE_KEYS
    assert body["state"] == "succeeded"


def test_charge_outcome_body_keys_are_unchanged_for_a_clean_charge(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _charge(h)

    # Assert
    assert set(outcome.body) == CHARGE_RESPONSE_KEYS


# --- reading the flags: get_payment_risk ---------------------------------------------------------------
# The score and reasons stay internal (security audit M1): the view only carries the two flags.


def _flagged_payment(h: Harness) -> uuid.UUID:
    _prime(h, APPROVE_PAN, 3)
    return _payment_id(_charge(h, amount_minor=HIGH_USD))


def _null_the_score(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> None:
    with session_factory() as session:
        session.execute(
            text("UPDATE gateway.payments SET risk_score = NULL WHERE id = :id"), {"id": payment_id}
        )
        session.commit()


def test_payment_risk_view_has_only_the_flags_and_the_payment_id() -> None:
    # Arrange / Act
    fields = set(PaymentRiskView.model_fields)

    # Assert
    assert fields == {"payment_id", "risk_flagged", "blocked"}


def test_get_payment_risk_of_a_flagged_charge_is_flagged_and_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _flagged_payment(h)

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=True, blocked=False)


def test_get_payment_risk_of_a_clean_charge_is_neither_flagged_nor_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h))

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=False, blocked=False)


def test_get_payment_risk_of_a_blocked_charge_is_flagged_and_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_blocked_charge(h))

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=True, blocked=True)


def test_get_payment_risk_of_an_ordinary_provider_decline_is_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h, pan=DECLINED_PAN))

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=False, blocked=False)


def test_get_payment_risk_of_a_payment_from_before_the_change_is_neither_flagged_nor_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h))
    _null_the_score(session_factory, payment_id)

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=False, blocked=False)


def test_get_payment_risk_for_an_unknown_payment_raises_payment_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)

    # Act / Assert
    with session_factory() as session, pytest.raises(PaymentNotFoundError):
        get_payment_risk(session, h.merchant_id, uuid.uuid4())


def test_get_payment_risk_for_another_merchants_payment_raises_payment_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, shared=h, name="Other")
    payment_id = _payment_id(_charge(h))

    # Act / Assert
    with session_factory() as session, pytest.raises(PaymentNotFoundError):
        get_payment_risk(session, other.merchant_id, payment_id)


# --- reading the flags: GET /v1/payments/{payment_id}/risk ---------------------------------------------


def test_get_payment_risk_route_returns_200_with_payment_id_and_the_two_flags(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _flagged_payment(h)
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"payment_id": str(payment_id), "risk_flagged": True, "blocked": False}


def test_get_payment_risk_route_never_exposes_the_score_or_the_reasons(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: the stored score and reasons exist, but must not reach the merchant (security audit M1)
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_blocked_charge(h))
    stored = _risk_row(session_factory, payment_id)
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=h.auth)

    # Assert
    body = response.json()
    assert (stored["risk_score"], stored["risk_reasons"]) == (
        Decimal("1.00"),
        ["high_amount", "card_velocity", "recent_declines"],
    )
    assert "risk_score" not in body
    assert "risk_reasons" not in body
    assert set(body) == {"payment_id", "risk_flagged", "blocked"}
    assert "card_velocity" not in response.text
    assert "1.00" not in response.text


def test_get_payment_risk_route_for_a_clean_charge_returns_false_false(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h))
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"payment_id": str(payment_id), "risk_flagged": False, "blocked": False}


def test_get_payment_risk_route_for_a_blocked_charge_reports_flagged_and_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_blocked_charge(h))
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"payment_id": str(payment_id), "risk_flagged": True, "blocked": True}


def test_get_payment_risk_route_for_a_payment_from_before_the_change_returns_false_false(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h))
    _null_the_score(session_factory, payment_id)
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"payment_id": str(payment_id), "risk_flagged": False, "blocked": False}


def test_get_payment_risk_route_for_an_unknown_payment_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{uuid.uuid4()}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "payment not found"}


def test_get_payment_risk_route_for_another_merchants_payment_returns_the_same_404(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, shared=h, name="Other")
    payment_id = _payment_id(_charge(h))
    client = _client(other)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=other.auth)

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "payment not found"}


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_unknown"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_get_payment_risk_route_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h))
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=headers)

    # Assert
    assert response.status_code == 401


@pytest.mark.parametrize("payment_id", ["not-a-uuid", "123", "00000000-0000-0000-0000-00000000000z"])
def test_get_payment_risk_route_with_a_non_uuid_id_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, payment_id: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}/risk", headers=h.auth)

    # Assert
    assert response.status_code == 422


# --- history stays as in B3 ----------------------------------------------------------------------------


def test_payment_summary_and_get_payments_carry_no_risk_fields(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _flagged_payment(h)
    client = _client(h)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)
    items = client.get("/v1/payments", headers=h.auth).json()["items"]

    # Assert
    assert set(PaymentSummary.model_fields) == HISTORY_ITEM_KEYS
    assert len(page.items) == 4
    assert [set(item) for item in items] == [HISTORY_ITEM_KEYS] * 4
