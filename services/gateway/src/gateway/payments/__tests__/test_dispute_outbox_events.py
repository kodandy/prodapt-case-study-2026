"""Each dispute workflow change writes one gw.disputes.v1 outbox row in the same transaction (R2-1 / R2-2)."""

import uuid
from dataclasses import dataclass

import pytest
from contracts.events import DISPUTES_TOPIC, DisputeEventV1
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import (
    CHARGEBACK_FEE_MINOR,
    DisputeAmountExceedsError,
    InvalidDisputeTransitionError,
    open_dispute,
    open_dispute_in_session,
    resolve_dispute,
    submit_evidence,
)
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
EVIDENCE_TEXT = "Delivered to Jane Roe at 1 Main St, signed"
ACTOR = "ops_lead:test"
DISPUTE_ROWS = "SELECT event_id, payload FROM gateway.outbox WHERE topic = :topic ORDER BY id"


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID


def _harness(session_factory: sessionmaker[Session], clock: FixedClock) -> Harness:
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(deps, session_factory, clock, credentials.merchant_id)


def _charge(h: Harness, amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _dispute_events(session_factory: sessionmaker[Session]) -> list[DisputeEventV1]:
    with session_factory() as session:
        rows = session.execute(text(DISPUTE_ROWS), {"topic": DISPUTES_TOPIC}).mappings().all()
    events = [DisputeEventV1.model_validate(row["payload"]) for row in rows]
    assert [row["event_id"] for row in rows] == [event.event_id for event in events]
    return events


def _raw_payloads(session_factory: sessionmaker[Session]) -> list[str]:
    with session_factory() as session:
        return list(
            session.execute(
                text("SELECT payload::text FROM gateway.outbox WHERE topic = :topic"),
                {"topic": DISPUTES_TOPIC},
            ).scalars()
        )


def test_opening_a_dispute_writes_one_opened_event_with_the_payment_amounts(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    view = open_dispute(session_factory, clock, payment_id, "13.1", 1000)

    # Assert
    events = _dispute_events(session_factory)
    assert [(e.event_type, e.entity_version, e.from_state, e.to_state, e.actor_role) for e in events] == [
        ("dispute.opened", 1, None, "opened", "card_network")
    ]
    event = events[0]
    assert (event.dispute_id, event.payment_id, event.merchant_id) == (
        view.dispute_id,
        payment_id,
        h.merchant_id,
    )
    assert (event.network_reason_code, event.currency) == ("13.1", "USD")
    assert (event.payment_amount_minor, event.refunded_minor, event.dispute_amount_minor) == (1500, 0, 1000)
    assert (event.chargeback_minor, event.chargeback_fee_minor) == (0, 0)
    assert event.occurred_at == clock()


def test_the_full_lost_workflow_writes_three_versioned_events_with_the_chargeback(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    view = open_dispute(session_factory, clock, payment_id, "10.4")

    # Act
    submit_evidence(session_factory, clock, h.merchant_id, view.dispute_id, EVIDENCE_TEXT)
    resolve_dispute(session_factory, clock, view.dispute_id, "lost", ACTOR)

    # Assert
    events = _dispute_events(session_factory)
    assert [(e.event_type, e.entity_version, e.from_state, e.to_state, e.actor_role) for e in events] == [
        ("dispute.opened", 1, None, "opened", "card_network"),
        ("dispute.evidence_submitted", 2, "opened", "under_review", "merchant"),
        ("dispute.resolved", 3, "under_review", "lost", "ops_lead"),
    ]
    resolved = events[-1]
    assert (resolved.chargeback_minor, resolved.chargeback_fee_minor) == (1500, CHARGEBACK_FEE_MINOR["USD"])
    assert len({event.event_id for event in events}) == 3


def test_a_network_resolution_is_attributed_to_the_card_network(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = open_dispute(session_factory, clock, _charge(h), "13.1")
    submit_evidence(session_factory, clock, h.merchant_id, view.dispute_id, EVIDENCE_TEXT)

    # Act
    resolve_dispute(session_factory, clock, view.dispute_id, "won", "network", "card_network")

    # Assert
    resolved = _dispute_events(session_factory)[-1]
    assert (resolved.to_state, resolved.actor_role) == ("won", "card_network")
    assert (resolved.chargeback_minor, resolved.chargeback_fee_minor) == (0, 0)


def test_a_consumer_opened_dispute_with_a_free_text_reason_carries_no_reason_code_and_no_payer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    payer = uuid.uuid4()

    # Act
    open_dispute(
        session_factory,
        clock,
        payment_id,
        "not me - Jane Roe",
        actor_id=f"user:{payer}",
        actor_role="consumer",
    )

    # Assert
    events = _dispute_events(session_factory)
    assert [(e.actor_role, e.network_reason_code) for e in events] == [("consumer", None)]
    assert all(
        "Jane Roe" not in payload and str(payer) not in payload for payload in _raw_payloads(session_factory)
    )


def test_the_evidence_text_never_reaches_the_event(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = open_dispute(session_factory, clock, _charge(h), "13.1")

    # Act
    submit_evidence(session_factory, clock, h.merchant_id, view.dispute_id, EVIDENCE_TEXT)

    # Assert
    payloads = _raw_payloads(session_factory)
    assert len(payloads) == 2
    assert [EVIDENCE_TEXT in payload or "Jane Roe" in payload for payload in payloads] == [False, False]


def test_a_rejected_open_writes_no_dispute_event(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    with pytest.raises(DisputeAmountExceedsError):
        open_dispute(session_factory, clock, payment_id, "13.1", 999_999)

    # Assert
    assert _dispute_events(session_factory) == []


def test_a_rejected_transition_writes_no_further_event(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = open_dispute(session_factory, clock, _charge(h), "13.1")

    # Act
    with pytest.raises(InvalidDisputeTransitionError):
        resolve_dispute(session_factory, clock, view.dispute_id, "won", ACTOR)

    # Assert
    assert [event.event_type for event in _dispute_events(session_factory)] == ["dispute.opened"]


def test_the_event_rolls_back_with_the_callers_transaction(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    with session_factory() as session:
        open_dispute_in_session(session, clock, payment_id, "13.1")
        session.flush()
        pending = session.execute(
            text("SELECT count(*) FROM gateway.outbox WHERE topic = :topic"), {"topic": DISPUTES_TOPIC}
        ).scalar_one()
        session.rollback()

    # Assert
    assert pending == 1
    assert _dispute_events(session_factory) == []
    with session_factory() as session:
        disputes = session.execute(text("SELECT count(*) FROM gateway.disputes")).scalar_one()
    assert disputes == 0
