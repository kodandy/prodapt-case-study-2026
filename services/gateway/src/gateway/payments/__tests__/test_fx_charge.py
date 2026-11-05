"""Tests for `charge_card` with `ChargeRequest.settlement_currency`, written from docs/specs/gateway-basic.md
section "B1.4 / B2.3 / B2.5 / P0-2 / P0-3" (Decision 6: cross-currency card charge).

Needs the 12 migration-seeded demo rates (`gateway.fx_rates`) to survive the per-test table reset.
"""

import dataclasses
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, ChargeOutcome, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import CardSummary, ChargeRequest, ChargeResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
CARD_DECLINED_PAN = "4000000000000002"


@dataclasses.dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    simulator: SimulatorProvider
    session_factory: sessionmaker[Session]
    merchant_id: uuid.UUID


def _harness(session_factory: sessionmaker[Session], clock: FixedClock) -> Harness:
    merchant_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Merchant(id=merchant_id, name="Acme", created_at=clock()))
        session.commit()
    simulator = SimulatorProvider(session_factory, clock)
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=simulator,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return Harness(deps, simulator, session_factory, merchant_id)


def _request(
    pan: str = APPROVE_PAN,
    amount_minor: int = 1_501,
    currency: str = "USD",
    settlement_currency: str | None = None,
) -> ChargeRequest:
    payload: dict[str, object] = {
        "amount_minor": amount_minor,
        "currency": currency,
        "method": "card",
        "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }
    if settlement_currency is not None:
        payload["settlement_currency"] = settlement_currency
    return ChargeRequest.model_validate(payload)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    return int(str(_rows(session_factory, f"SELECT count(*) AS n FROM {table}")[0]["n"]))  # noqa: S608


def _payment_journal(session_factory: sessionmaker[Session], payment_id: object) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT j.ref_type, j.fx_rate_id, p.currency, p.amount_minor, a.owner_type, a.owner_id, a.kind "
        "FROM gateway.journal_entries j JOIN gateway.postings p ON p.journal_id = j.id "
        "JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "WHERE j.ref_id = :ref_id ORDER BY p.currency, p.amount_minor",
        ref_id=uuid.UUID(str(payment_id)),
    )


def _expected_body(
    body: dict[str, object], *, state: str, decline_code: str | None, amount: int, currency: str
) -> dict[str, object]:
    return ChargeResponse(
        payment_id=uuid.UUID(str(body["payment_id"])),
        state=state,
        amount_minor=amount,
        currency=currency,
        method="card",
        card=CardSummary(bin="424242", last4="4242"),
        decline_code=decline_code,
    ).model_dump(mode="json")


def _insert_rate(
    session_factory: sessionmaker[Session],
    rate_id: str,
    base: str,
    quoted: str,
    rate: str,
    valid_from: datetime,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO gateway.fx_rates (id, base_currency, quote_currency, rate, valid_from) "
                "VALUES (:id, :base, :quote, :rate, :valid_from)"
            ),
            {
                "id": rate_id,
                "base": base,
                "quote": quoted,
                "rate": Decimal(rate),
                "valid_from": valid_from,
            },
        )


# --- request schema ------------------------------------------------------------------------------------


def test_charge_request_settlement_currency_defaults_to_none() -> None:
    # Arrange / Act
    request = _request()

    # Assert
    assert request.settlement_currency is None


@pytest.mark.parametrize("settlement_currency", ["USD", "EUR", "JPY", "INR"])
def test_charge_request_accepts_each_supported_settlement_currency(settlement_currency: str) -> None:
    # Arrange / Act
    request = _request(settlement_currency=settlement_currency)

    # Assert
    assert request.settlement_currency == settlement_currency


@pytest.mark.parametrize("settlement_currency", ["GBP", "usd", "", "US"])
def test_charge_request_rejects_an_unsupported_settlement_currency(settlement_currency: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValidationError):
        _request(settlement_currency=settlement_currency)


# --- succeeded cross-currency charge -------------------------------------------------------------------


def test_cross_currency_charge_posts_a_four_line_fx_journal_with_the_rate_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps, h.merchant_id, "k-fx",
        _request(amount_minor=1_501, settlement_currency="EUR"),
    )

    # Assert: 1501 * 0.92 = 1380.92 -> 1380
    assert outcome.status_code == 201
    assert _count(session_factory, "gateway.journal_entries") == 1
    assert _payment_journal(session_factory, outcome.body["payment_id"]) == [
        {
            "ref_type": "payment",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "EUR",
            "amount_minor": -1_380,
            "owner_type": "merchant",
            "owner_id": h.merchant_id,
            "kind": "merchant_payable",
        },
        {
            "ref_type": "payment",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "EUR",
            "amount_minor": 1_380,
            "owner_type": "platform",
            "owner_id": None,
            "kind": "fx_clearing",
        },
        {
            "ref_type": "payment",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "USD",
            "amount_minor": -1_501,
            "owner_type": "platform",
            "owner_id": None,
            "kind": "fx_clearing",
        },
        {
            "ref_type": "payment",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "USD",
            "amount_minor": 1_501,
            "owner_type": "platform",
            "owner_id": None,
            "kind": "provider_receivable",
        },
    ]


@pytest.mark.parametrize(
    ("settlement_currency", "settled_minor"),
    [
        pytest.param("EUR", 1_380, id="eur-floor-1380.92"),
        pytest.param("JPY", 2_274, id="jpy-no-minor-digits-floor-2274.015"),
        pytest.param("INR", 124_958, id="inr-floor-124958.25"),
    ],
)
def test_cross_currency_charge_credits_the_merchant_the_converted_floor_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, settlement_currency: str, settled_minor: int
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps,
        h.merchant_id,
        "k-fx-amt",
        _request(amount_minor=1_501, settlement_currency=settlement_currency),
    )

    # Assert
    payable = _rows(
        session_factory,
        "SELECT p.currency, p.amount_minor, j.fx_rate_id FROM gateway.postings p "
        "JOIN gateway.journal_entries j ON j.id = p.journal_id "
        "JOIN gateway.ledger_accounts a ON a.id = p.account_id WHERE a.kind = 'merchant_payable'",
    )
    assert outcome.status_code == 201
    assert payable == [
        {
            "currency": settlement_currency,
            "amount_minor": -settled_minor,
            "fx_rate_id": f"demo-USD{settlement_currency}-v1",
        }
    ]


def test_cross_currency_charge_leaves_each_currency_netting_to_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    charge_card(h.deps, h.merchant_id, "k-fx-net", _request(amount_minor=1_501, settlement_currency="JPY"))

    # Assert
    totals = _rows(
        session_factory,
        "SELECT currency, sum(amount_minor)::bigint AS total FROM gateway.postings GROUP BY currency "
        "ORDER BY currency",
    )
    assert totals == [{"currency": "JPY", "total": 0}, {"currency": "USD", "total": 0}]


def test_cross_currency_charge_response_body_is_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps, h.merchant_id, "k-fx-body",
        _request(amount_minor=1_501, settlement_currency="EUR"),
    )

    # Assert
    assert outcome.body == _expected_body(
        outcome.body, state="succeeded", decline_code=None, amount=1_501, currency="USD"
    )
    assert sorted(outcome.body) == [
        "amount_minor",
        "card",
        "currency",
        "decline_code",
        "method",
        "payment_id",
        "state",
    ]


def test_cross_currency_charge_stores_the_payment_in_the_card_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps, h.merchant_id, "k-fx-pay",
        _request(amount_minor=1_501, settlement_currency="EUR"),
    )

    # Assert
    payments = _rows(session_factory, "SELECT id, amount_minor, currency, state FROM gateway.payments")
    assert payments == [
        {
            "id": uuid.UUID(str(outcome.body["payment_id"])),
            "amount_minor": 1_501,
            "currency": "USD",
            "state": "succeeded",
        }
    ]
    assert h.simulator.charge_count() == 1


def test_cross_currency_charge_uses_the_rate_in_force_at_the_clock_time(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _insert_rate(session_factory, "test-USDEUR-v2", "USD", "EUR", "0.50", datetime(2026, 11, 1, tzinfo=UTC))

    # Act
    outcome = charge_card(
        h.deps, h.merchant_id, "k-fx-v2",
        _request(amount_minor=1_000, settlement_currency="EUR"),
    )

    # Assert
    journal = _payment_journal(session_factory, outcome.body["payment_id"])
    assert {line["fx_rate_id"] for line in journal} == {"test-USDEUR-v2"}
    assert [(line["currency"], line["amount_minor"]) for line in journal] == [
        ("EUR", -500),
        ("EUR", 500),
        ("USD", -1_000),
        ("USD", 1_000),
    ]


def test_cross_currency_charge_replay_returns_the_stored_body_and_posts_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    request = _request(amount_minor=1_501, settlement_currency="EUR")
    first = charge_card(h.deps, h.merchant_id, "k-fx-replay", request)

    # Act
    second = charge_card(h.deps, h.merchant_id, "k-fx-replay", request)

    # Assert
    assert second == first
    assert h.simulator.charge_count() == 1
    assert _count(session_factory, "gateway.journal_entries") == 1
    assert _count(session_factory, "gateway.postings") == 4


# --- declined ------------------------------------------------------------------------------------------


def test_declined_cross_currency_charge_posts_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps,
        h.merchant_id,
        "k-fx-decline",
        _request(pan=CARD_DECLINED_PAN, amount_minor=1_501, settlement_currency="EUR"),
    )

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["state"] == "declined"
    assert outcome.body["decline_code"] == "card_declined"
    assert _count(session_factory, "gateway.journal_entries") == 0
    assert _count(session_factory, "gateway.postings") == 0
    assert _rows(session_factory, "SELECT state, amount_minor, currency FROM gateway.payments") == [
        {"state": "declined", "amount_minor": 1_501, "currency": "USD"}
    ]
    assert h.simulator.charge_count() == 1


# --- settlement currency absent or equal: unchanged ----------------------------------------------------


@pytest.mark.parametrize("settlement_currency", [None, "USD"], ids=["absent", "equal-to-currency"])
def test_same_currency_charge_posts_the_unchanged_two_line_journal_without_an_fx_rate(
    session_factory: sessionmaker[Session], clock: FixedClock, settlement_currency: str | None
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps, h.merchant_id, "k-same", _request(amount_minor=1_501, settlement_currency=settlement_currency)
    )

    # Assert
    assert outcome.status_code == 201
    assert _payment_journal(session_factory, outcome.body["payment_id"]) == [
        {
            "ref_type": "payment",
            "fx_rate_id": None,
            "currency": "USD",
            "amount_minor": -1_501,
            "owner_type": "merchant",
            "owner_id": h.merchant_id,
            "kind": "merchant_payable",
        },
        {
            "ref_type": "payment",
            "fx_rate_id": None,
            "currency": "USD",
            "amount_minor": 1_501,
            "owner_type": "platform",
            "owner_id": None,
            "kind": "provider_receivable",
        },
    ]
    assert outcome.body == _expected_body(
        outcome.body, state="succeeded", decline_code=None, amount=1_501, currency="USD"
    )


# --- fx errors: nothing claimed, no provider call ------------------------------------------------------


def test_charge_whose_converted_amount_is_zero_answers_422_and_claims_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: 1 JPY * 0.0066006600 * 100 = 0.66 USD minor units -> 0
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps, h.merchant_id, "k-small", _request(amount_minor=1, currency="JPY", settlement_currency="USD")
    )

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "fx_amount_too_small"})
    assert _count(session_factory, "gateway.payments") == 0
    assert _count(session_factory, "gateway.payment_attempts") == 0
    assert _count(session_factory, "gateway.idempotency_keys") == 0
    assert _count(session_factory, "gateway.journal_entries") == 0
    assert _count(session_factory, "gateway.outbox") == 0
    assert _count(session_factory, "gateway.audit_log") == 0
    assert h.simulator.charge_count() == 0


def test_a_charge_rejected_for_a_too_small_amount_does_not_burn_its_idempotency_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    rejected = charge_card(
        h.deps,
        h.merchant_id,
        "k-small-retry",
        _request(amount_minor=1, currency="JPY", settlement_currency="USD"),
    )

    # Act
    retried = charge_card(h.deps, h.merchant_id, "k-small-retry", _request(amount_minor=1, currency="JPY"))

    # Assert
    assert rejected.status_code == 422
    assert retried.status_code == 201
    assert retried.body["state"] == "succeeded"
    assert h.simulator.charge_count() == 1


def test_a_card_that_would_decline_still_gets_the_422_before_any_provider_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = charge_card(
        h.deps,
        h.merchant_id,
        "k-small-decline",
        _request(pan=CARD_DECLINED_PAN, amount_minor=1, currency="JPY", settlement_currency="USD"),
    )

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "fx_amount_too_small"})
    assert _count(session_factory, "gateway.payments") == 0
    assert h.simulator.charge_count() == 0


def test_charge_without_a_rate_in_force_answers_422_and_claims_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the seeded rates are valid from 2026-01-01
    h = _harness(session_factory, FixedClock(datetime(2025, 12, 31, 12, 0, tzinfo=UTC)))

    # Act
    outcome: ChargeOutcome = charge_card(
        h.deps, h.merchant_id, "k-norate", _request(amount_minor=1_501, settlement_currency="EUR")
    )

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "fx_rate_unavailable"})
    assert _count(session_factory, "gateway.payments") == 0
    assert _count(session_factory, "gateway.idempotency_keys") == 0
    assert _count(session_factory, "gateway.journal_entries") == 0
    assert h.simulator.charge_count() == 0
