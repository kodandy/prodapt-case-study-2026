"""RED tests for B3 disputes, from docs/specs/gateway-basic.md (disputes.py + /v1/disputes routes)."""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import (
    CHARGEBACK_FEE_MINOR,
    DisputeAlreadyOpenError,
    DisputeAmountExceedsError,
    DisputeNotFoundError,
    InvalidDisputeTransitionError,
    PaymentNotDisputableError,
    PaymentNotFoundError,
    open_dispute,
    resolve_dispute,
    submit_evidence,
)
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.refunds import RefundOutcome, refund_payment
from gateway.payments.schemas import ChargeRequest, DisputeView
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
ACTOR = "ops_lead:test"


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    auth: dict[str, str]


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    deps: ChargeDeps | None = None,
    name: str = "Acme",
) -> Harness:
    charge_deps = deps or ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(
        charge_deps,
        session_factory,
        clock,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
    )


def _client(h: Harness) -> TestClient:
    return TestClient(create_app(GatewayAppDeps(charge=h.deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _charge(h: Harness, pan: str = APPROVE_PAN, amount_minor: int = 1500, currency: str = "USD") -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": currency,
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _open(
    h: Harness, payment_id: uuid.UUID, amount_minor: int | None = None, reason: str = "fraudulent"
) -> DisputeView:
    return open_dispute(h.session_factory, h.clock, payment_id, reason, amount_minor)


def _under_review(h: Harness, payment_id: uuid.UUID, amount_minor: int | None = None) -> DisputeView:
    opened = _open(h, payment_id, amount_minor)
    return submit_evidence(h.session_factory, h.clock, h.merchant_id, opened.dispute_id, "receipt attached")


def _resolve(h: Harness, dispute_id: uuid.UUID, outcome: Literal["won", "lost"]) -> DisputeView:
    return resolve_dispute(h.session_factory, h.clock, dispute_id, outcome, ACTOR)


def _refund(h: Harness, payment_id: uuid.UUID, key: str, amount_minor: int | None = None) -> RefundOutcome:
    return refund_payment(h.session_factory, h.clock, h.merchant_id, payment_id, key, amount_minor)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _totals(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> tuple[int, int, int]:
    row = _rows(
        session_factory,
        "SELECT refunded_minor, disputed_minor, chargeback_minor FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]
    return int(str(row["refunded_minor"])), int(str(row["disputed_minor"])), int(str(row["chargeback_minor"]))


def _dispute_row(session_factory: sessionmaker[Session], dispute_id: uuid.UUID) -> dict[str, object]:
    return _rows(
        session_factory,
        "SELECT state, evidence_text, chargeback_fee_minor, created_at, updated_at, resolved_at "
        "FROM gateway.disputes WHERE id = :id",
        id=dispute_id,
    )[0]


def _journal_count(session_factory: sessionmaker[Session], ref_type: str) -> int:
    rows = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = :ref_type",
        ref_type=ref_type,
    )
    return int(str(rows[0]["n"]))


def _balance(
    session_factory: sessionmaker[Session],
    owner_type: str,
    owner_id: uuid.UUID | None,
    kind: str,
    currency: str,
) -> int:
    rows = _rows(
        session_factory,
        "SELECT coalesce(sum(p.amount_minor), 0) AS balance FROM gateway.postings p "
        "JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "WHERE a.owner_type = :owner_type AND a.owner_id IS NOT DISTINCT FROM :owner_id "
        "AND a.kind = :kind AND a.currency = :currency",
        owner_type=owner_type,
        owner_id=owner_id,
        kind=kind,
        currency=currency,
    )
    return int(str(rows[0]["balance"]))


def _ledger_sum_by_currency(session_factory: sessionmaker[Session]) -> dict[str, int]:
    rows = _rows(
        session_factory, "SELECT currency, sum(amount_minor) AS total FROM gateway.postings GROUP BY currency"
    )
    return {str(row["currency"]): int(str(row["total"])) for row in rows}


def _audit_actions(session_factory: sessionmaker[Session], action: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, action, target, summary FROM gateway.audit_log WHERE action = :action ORDER BY id",
        action=action,
    )


# --- open_dispute --------------------------------------------------------------------------------------


def test_open_dispute_without_an_amount_disputes_the_full_refundable_amount(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    view = _open(h, payment_id, reason="fraudulent")

    # Assert
    assert view == DisputeView(
        dispute_id=view.dispute_id,
        payment_id=payment_id,
        state="opened",
        amount_minor=1500,
        currency="USD",
        reason="fraudulent",
        chargeback_fee_minor=0,
        created_at=clock(),
        updated_at=clock(),
    )
    assert _totals(session_factory, payment_id) == (0, 1500, 0)
    assert _dispute_row(session_factory, view.dispute_id)["state"] == "opened"


def test_open_dispute_with_a_partial_amount_reserves_only_that_amount(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    view = _open(h, payment_id, amount_minor=700)

    # Assert
    assert view.amount_minor == 700
    assert _totals(session_factory, payment_id) == (0, 700, 0)


def test_open_dispute_after_a_partial_refund_defaults_to_the_remaining_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-before-dispute", 1000)

    # Act
    view = _open(h, payment_id)

    # Assert
    assert view.amount_minor == 500
    assert _totals(session_factory, payment_id) == (1000, 500, 0)


def test_open_dispute_writes_a_dispute_opened_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    view = _open(h, payment_id)

    # Assert
    audit = _audit_actions(session_factory, "dispute.opened")
    assert [(row["action"], row["target"]) for row in audit] == [
        ("dispute.opened", f"dispute:{view.dispute_id}")
    ]


def test_open_dispute_for_an_unknown_payment_raises_payment_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act / Assert
    with pytest.raises(PaymentNotFoundError):
        _open(h, uuid.uuid4())


def test_open_dispute_for_a_declined_payment_raises_payment_not_disputable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, pan=DECLINED_PAN)

    # Act / Assert
    with pytest.raises(PaymentNotDisputableError):
        _open(h, payment_id)
    assert _totals(session_factory, payment_id) == (0, 0, 0)


def test_open_dispute_while_one_is_opened_raises_dispute_already_open(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _open(h, payment_id, amount_minor=500)

    # Act / Assert
    with pytest.raises(DisputeAlreadyOpenError):
        _open(h, payment_id, amount_minor=100)
    assert _totals(session_factory, payment_id) == (0, 500, 0)


def test_open_dispute_while_one_is_under_review_raises_dispute_already_open(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _under_review(h, payment_id, amount_minor=500)

    # Act / Assert
    with pytest.raises(DisputeAlreadyOpenError):
        _open(h, payment_id, amount_minor=100)


@pytest.mark.parametrize("amount_minor", [0, -1, 1501], ids=["zero", "negative", "above_refundable"])
def test_open_dispute_with_an_invalid_amount_raises_dispute_amount_exceeds(
    session_factory: sessionmaker[Session], clock: FixedClock, amount_minor: int
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act / Assert
    with pytest.raises(DisputeAmountExceedsError):
        _open(h, payment_id, amount_minor=amount_minor)
    assert _totals(session_factory, payment_id) == (0, 0, 0)


def test_open_dispute_above_what_a_refund_left_raises_dispute_amount_exceeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-before-dispute", 1000)

    # Act / Assert
    with pytest.raises(DisputeAmountExceedsError):
        _open(h, payment_id, amount_minor=501)


def test_open_dispute_on_a_fully_refunded_payment_raises_dispute_amount_exceeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-all")

    # Act / Assert
    with pytest.raises(DisputeAmountExceedsError):
        _open(h, payment_id)


def test_open_dispute_is_allowed_again_once_the_previous_one_is_resolved(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    first = _under_review(h, payment_id, amount_minor=500)
    _resolve(h, first.dispute_id, "won")

    # Act
    second = _open(h, payment_id, amount_minor=1500)

    # Assert
    assert second.state == "opened"
    assert second.dispute_id != first.dispute_id
    assert _totals(session_factory, payment_id) == (0, 1500, 0)


# --- submit_evidence -----------------------------------------------------------------------------------


def test_submit_evidence_moves_opened_to_under_review_and_stores_the_text(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    opened = _open(h, payment_id)
    clock.advance(timedelta(hours=1))

    # Act
    view = submit_evidence(
        h.session_factory, h.clock, h.merchant_id, opened.dispute_id, "signed delivery slip"
    )

    # Assert
    assert view == opened.model_copy(update={"state": "under_review", "updated_at": clock()})
    row = _dispute_row(session_factory, opened.dispute_id)
    assert (row["state"], row["evidence_text"]) == ("under_review", "signed delivery slip")


def test_submit_evidence_writes_a_dispute_evidence_submitted_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))

    # Act
    submit_evidence(h.session_factory, h.clock, h.merchant_id, opened.dispute_id, "receipt")

    # Assert
    audit = _audit_actions(session_factory, "dispute.evidence_submitted")
    assert [(row["actor_id"], row["target"]) for row in audit] == [
        (f"merchant:{h.merchant_id}", f"dispute:{opened.dispute_id}")
    ]


def test_submit_evidence_for_an_unknown_dispute_raises_dispute_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act / Assert
    with pytest.raises(DisputeNotFoundError):
        submit_evidence(h.session_factory, h.clock, h.merchant_id, uuid.uuid4(), "receipt")


def test_submit_evidence_for_another_merchants_dispute_raises_dispute_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    opened = _open(h, _charge(h))

    # Act / Assert
    with pytest.raises(DisputeNotFoundError):
        submit_evidence(other.session_factory, other.clock, other.merchant_id, opened.dispute_id, "receipt")
    assert _dispute_row(session_factory, opened.dispute_id)["state"] == "opened"


def test_submit_evidence_twice_raises_invalid_dispute_transition(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = _under_review(h, _charge(h))

    # Act / Assert
    with pytest.raises(InvalidDisputeTransitionError):
        submit_evidence(h.session_factory, h.clock, h.merchant_id, view.dispute_id, "second attempt")
    assert _dispute_row(session_factory, view.dispute_id)["evidence_text"] == "receipt attached"


def test_submit_evidence_after_resolution_raises_invalid_dispute_transition(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = _under_review(h, _charge(h))
    _resolve(h, view.dispute_id, "won")

    # Act / Assert
    with pytest.raises(InvalidDisputeTransitionError):
        submit_evidence(h.session_factory, h.clock, h.merchant_id, view.dispute_id, "too late")


# --- resolve_dispute -----------------------------------------------------------------------------------


def test_resolve_dispute_won_releases_the_reserved_amount_without_a_journal(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    reviewed = _under_review(h, payment_id)
    clock.advance(timedelta(days=3))
    journals_before = _journal_count(session_factory, "dispute")

    # Act
    view = _resolve(h, reviewed.dispute_id, "won")

    # Assert
    assert view == reviewed.model_copy(update={"state": "won", "updated_at": clock()})
    row = _dispute_row(session_factory, reviewed.dispute_id)
    assert (row["state"], row["resolved_at"], row["chargeback_fee_minor"]) == ("won", clock(), 0)
    assert _totals(session_factory, payment_id) == (0, 0, 0)
    assert (journals_before, _journal_count(session_factory, "dispute")) == (0, 0)
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


def test_resolve_dispute_lost_moves_the_amount_to_chargeback_and_charges_the_fee(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    reviewed = _under_review(h, payment_id)
    clock.advance(timedelta(days=3))

    # Act
    view = _resolve(h, reviewed.dispute_id, "lost")

    # Assert
    assert view == reviewed.model_copy(
        update={"state": "lost", "chargeback_fee_minor": 1500, "updated_at": clock()}
    )
    row = _dispute_row(session_factory, reviewed.dispute_id)
    assert (row["state"], row["resolved_at"], row["chargeback_fee_minor"]) == ("lost", clock(), 1500)
    assert _totals(session_factory, payment_id) == (0, 0, 1500)


def test_resolve_dispute_lost_posts_one_dispute_journal_with_amount_and_fee_lines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h, amount_minor=1500), amount_minor=1000)

    # Act
    _resolve(h, reviewed.dispute_id, "lost")

    # Assert
    journals = _rows(
        session_factory, "SELECT id, ref_type, ref_id FROM gateway.journal_entries WHERE ref_type = 'dispute'"
    )
    assert [(j["ref_type"], j["ref_id"]) for j in journals] == [("dispute", reviewed.dispute_id)]
    postings = _rows(
        session_factory,
        "SELECT a.owner_type, a.owner_id, a.kind, p.currency, p.amount_minor "
        "FROM gateway.postings p JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "WHERE p.journal_id = :journal_id ORDER BY p.amount_minor, a.kind",
        journal_id=journals[0]["id"],
    )
    assert postings == [
        {
            "owner_type": "platform",
            "owner_id": None,
            "kind": "chargeback_fee_revenue",
            "currency": "USD",
            "amount_minor": -1500,
        },
        {
            "owner_type": "platform",
            "owner_id": None,
            "kind": "provider_receivable",
            "currency": "USD",
            "amount_minor": -1000,
        },
        {
            "owner_type": "merchant",
            "owner_id": h.merchant_id,
            "kind": "merchant_payable",
            "currency": "USD",
            "amount_minor": 1000,
        },
        {
            "owner_type": "merchant",
            "owner_id": h.merchant_id,
            "kind": "merchant_payable",
            "currency": "USD",
            "amount_minor": 1500,
        },
    ]


def test_resolve_dispute_lost_leaves_the_balances_the_merchant_owes_and_the_platform_earns(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h, amount_minor=1500))

    # Act
    _resolve(h, reviewed.dispute_id, "lost")

    # Assert
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "USD") == 1500
    assert _balance(session_factory, "platform", None, "provider_receivable", "USD") == 0
    assert _balance(session_factory, "platform", None, "chargeback_fee_revenue", "USD") == -1500
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


@pytest.mark.parametrize(
    ("currency", "fee_minor"),
    [("USD", 1500), ("EUR", 1500), ("JPY", 2000), ("INR", 120000)],
)
def test_resolve_dispute_lost_charges_the_per_currency_fee_and_balances_the_journal(
    session_factory: sessionmaker[Session], clock: FixedClock, currency: str, fee_minor: int
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h, amount_minor=500000, currency=currency))

    # Act
    view = _resolve(h, reviewed.dispute_id, "lost")

    # Assert
    assert CHARGEBACK_FEE_MINOR[currency] == fee_minor
    assert view.chargeback_fee_minor == fee_minor
    assert _balance(session_factory, "platform", None, "chargeback_fee_revenue", currency) == -fee_minor
    assert _ledger_sum_by_currency(session_factory) == {currency: 0}


def test_chargeback_fee_table_matches_the_specified_amounts() -> None:
    # Arrange / Act / Assert
    assert CHARGEBACK_FEE_MINOR == {"USD": 1500, "EUR": 1500, "JPY": 2000, "INR": 120000}


def test_resolve_dispute_writes_a_dispute_resolved_audit_row_with_the_outcome_and_actor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h))

    # Act
    _resolve(h, reviewed.dispute_id, "lost")

    # Assert
    audit = _audit_actions(session_factory, "dispute.resolved")
    assert [(row["actor_id"], row["target"]) for row in audit] == [(ACTOR, f"dispute:{reviewed.dispute_id}")]
    summary = audit[0]["summary"]
    assert isinstance(summary, dict)
    assert "lost" in summary.values()


@pytest.mark.parametrize("second_outcome", ["won", "lost"])
def test_resolve_dispute_twice_raises_invalid_transition_and_posts_no_second_journal(
    session_factory: sessionmaker[Session], clock: FixedClock, second_outcome: Literal["won", "lost"]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    reviewed = _under_review(h, payment_id)
    _resolve(h, reviewed.dispute_id, "lost")

    # Act / Assert
    with pytest.raises(InvalidDisputeTransitionError):
        _resolve(h, reviewed.dispute_id, second_outcome)
    assert _journal_count(session_factory, "dispute") == 1
    assert _totals(session_factory, payment_id) == (0, 0, 1500)
    assert _dispute_row(session_factory, reviewed.dispute_id)["state"] == "lost"
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


def test_resolve_dispute_that_was_won_cannot_be_resolved_again(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h))
    _resolve(h, reviewed.dispute_id, "won")

    # Act / Assert
    with pytest.raises(InvalidDisputeTransitionError):
        _resolve(h, reviewed.dispute_id, "lost")
    assert _journal_count(session_factory, "dispute") == 0


@pytest.mark.parametrize("outcome", ["won", "lost"])
def test_resolve_dispute_before_evidence_raises_invalid_transition(
    session_factory: sessionmaker[Session], clock: FixedClock, outcome: Literal["won", "lost"]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    opened = _open(h, payment_id)

    # Act / Assert
    with pytest.raises(InvalidDisputeTransitionError):
        _resolve(h, opened.dispute_id, outcome)
    assert _dispute_row(session_factory, opened.dispute_id)["state"] == "opened"
    assert _totals(session_factory, payment_id) == (0, 1500, 0)
    assert _journal_count(session_factory, "dispute") == 0


# --- refund vs dispute interplay -----------------------------------------------------------------------


def test_refund_is_rejected_while_a_full_dispute_reserves_the_whole_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _open(h, payment_id)

    # Act
    outcome = _refund(h, payment_id, "refund-during-dispute", 1)

    # Assert
    assert outcome == RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": 0})
    assert _totals(session_factory, payment_id) == (0, 1500, 0)


def test_refund_may_only_take_what_a_partial_dispute_leaves(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _open(h, payment_id, amount_minor=1000)

    # Act
    too_much = _refund(h, payment_id, "refund-over", 501)
    fits = _refund(h, payment_id, "refund-fits")

    # Assert
    assert too_much == RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": 500})
    assert fits.status_code == 201
    assert fits.body["amount_minor"] == 500
    assert _totals(session_factory, payment_id) == (500, 1000, 0)


def test_refund_is_possible_again_after_the_dispute_is_won(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    reviewed = _under_review(h, payment_id)
    _resolve(h, reviewed.dispute_id, "won")

    # Act
    outcome = _refund(h, payment_id, "refund-after-win")

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["amount_minor"] == 1500
    assert _totals(session_factory, payment_id) == (1500, 0, 0)


def test_refund_stays_blocked_after_the_dispute_is_lost(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    reviewed = _under_review(h, payment_id)
    _resolve(h, reviewed.dispute_id, "lost")

    # Act
    outcome = _refund(h, payment_id, "refund-after-loss", 1)

    # Assert
    assert outcome == RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": 0})
    assert _totals(session_factory, payment_id) == (0, 0, 1500)


def test_dispute_and_refund_together_never_exceed_the_payment_amount_in_the_ledger(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _refund(h, payment_id, "refund-first", 500)
    reviewed = _under_review(h, payment_id)

    # Act
    _resolve(h, reviewed.dispute_id, "lost")

    # Assert
    assert reviewed.amount_minor == 1000
    assert _totals(session_factory, payment_id) == (500, 0, 1000)
    assert _balance(session_factory, "merchant", h.merchant_id, "merchant_payable", "USD") == 1500
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


# --- routes --------------------------------------------------------------------------------------------


def test_get_dispute_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.get(f"/v1/disputes/{opened.dispute_id}", headers={"Authorization": "Bearer sk_test_x"})

    # Assert
    assert response.status_code == 401


def test_get_dispute_returns_200_with_the_dispute_view(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h), reason="product_not_received")
    client = _client(h)

    # Act
    response = client.get(f"/v1/disputes/{opened.dispute_id}", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert set(response.json()) == {
        "dispute_id",
        "payment_id",
        "state",
        "amount_minor",
        "currency",
        "reason",
        "chargeback_fee_minor",
        "created_at",
        "updated_at",
    }
    assert DisputeView.model_validate(response.json()) == opened


def test_get_dispute_reflects_the_resolved_state_and_fee(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h))
    resolved = _resolve(h, reviewed.dispute_id, "lost")
    client = _client(h)

    # Act
    response = client.get(f"/v1/disputes/{reviewed.dispute_id}", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert DisputeView.model_validate(response.json()) == resolved
    assert (resolved.state, resolved.chargeback_fee_minor) == ("lost", 1500)


def test_get_dispute_unknown_or_another_merchants_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    theirs = _open(other, _charge(other))
    client = _client(h)

    # Act
    unknown = client.get(f"/v1/disputes/{uuid.uuid4()}", headers=h.auth)
    foreign = client.get(f"/v1/disputes/{theirs.dispute_id}", headers=h.auth)

    # Assert
    assert (unknown.status_code, foreign.status_code) == (404, 404)


def test_post_evidence_returns_200_with_the_under_review_view(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.post(
        f"/v1/disputes/{opened.dispute_id}/evidence", json={"text": "tracking number 123"}, headers=h.auth
    )

    # Assert
    assert response.status_code == 200
    assert DisputeView.model_validate(response.json()) == opened.model_copy(update={"state": "under_review"})
    assert _dispute_row(session_factory, opened.dispute_id)["evidence_text"] == "tracking number 123"


def test_post_evidence_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.post(f"/v1/disputes/{opened.dispute_id}/evidence", json={"text": "receipt"})

    # Assert
    assert response.status_code == 401
    assert _dispute_row(session_factory, opened.dispute_id)["state"] == "opened"


def test_post_evidence_for_unknown_or_another_merchants_dispute_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    theirs = _open(other, _charge(other))
    client = _client(h)

    # Act
    unknown = client.post(f"/v1/disputes/{uuid.uuid4()}/evidence", json={"text": "receipt"}, headers=h.auth)
    foreign = client.post(
        f"/v1/disputes/{theirs.dispute_id}/evidence", json={"text": "receipt"}, headers=h.auth
    )

    # Assert
    assert (unknown.status_code, foreign.status_code) == (404, 404)
    assert _dispute_row(session_factory, theirs.dispute_id)["state"] == "opened"


def test_post_evidence_twice_returns_409_invalid_dispute_transition(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)
    url = f"/v1/disputes/{opened.dispute_id}/evidence"
    client.post(url, json={"text": "first"}, headers=h.auth)

    # Act
    response = client.post(url, json={"text": "second"}, headers=h.auth)

    # Assert
    assert response.status_code == 409
    assert response.json() == {"detail": "invalid dispute transition"}
    assert _dispute_row(session_factory, opened.dispute_id)["evidence_text"] == "first"


@pytest.mark.parametrize(
    "body",
    [{"text": ""}, {"text": "x" * 4001}, {}, {"text": "ok", "state": "won"}],
    ids=["empty", "too_long", "missing", "extra_field"],
)
def test_post_evidence_with_an_invalid_body_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, body: dict[str, str]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.post(f"/v1/disputes/{opened.dispute_id}/evidence", json=body, headers=h.auth)

    # Assert
    assert response.status_code == 422
    assert _dispute_row(session_factory, opened.dispute_id)["state"] == "opened"


@pytest.mark.parametrize("length", [1, 4000])
def test_post_evidence_accepts_text_at_the_length_bounds(
    session_factory: sessionmaker[Session], clock: FixedClock, length: int
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.post(
        f"/v1/disputes/{opened.dispute_id}/evidence", json={"text": "x" * length}, headers=h.auth
    )

    # Assert
    assert response.status_code == 200
    assert _dispute_row(session_factory, opened.dispute_id)["evidence_text"] == "x" * length


def test_there_is_no_http_route_for_the_merchant_to_open_or_resolve_a_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    reviewed = _under_review(h, _charge(h))
    client = _client(h)

    # Act
    open_response = client.post(
        "/v1/disputes", json={"payment_id": str(payment_id), "reason": "fraudulent"}, headers=h.auth
    )
    resolve_response = client.post(
        f"/v1/disputes/{reviewed.dispute_id}/resolve", json={"outcome": "won"}, headers=h.auth
    )

    # Assert
    assert open_response.status_code in {404, 405}
    assert resolve_response.status_code in {404, 405}
    assert _dispute_row(session_factory, reviewed.dispute_id)["state"] == "under_review"
    assert _totals(session_factory, payment_id) == (0, 0, 0)
