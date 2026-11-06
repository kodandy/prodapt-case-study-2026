"""gw.disputes.v1 actor labels fail closed and the reason code follows the opener (audit L-1, L-2)."""

import uuid
from dataclasses import dataclass

import pytest
from contracts.events import DISPUTE_MAX_AMOUNT_MINOR, DISPUTES_TOPIC, DisputeEventV1
from gateway.accounts.service import create_merchant, sign_up
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import (
    UnknownDisputeActorError,
    open_dispute,
    resolve_dispute,
    submit_evidence,
)
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import MAX_AMOUNT_MINOR, ChargeRequest
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
TEST_PASSWORD = "dispute-actor-test-pw-41"
EVIDENCE_TEXT = "receipt attached"
DISPUTE_ROWS = "SELECT payload FROM gateway.outbox WHERE topic = :topic ORDER BY id"


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


def _charge(h: Harness) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _paid_by_user(h: Harness) -> tuple[uuid.UUID, uuid.UUID]:
    payment_id = _charge(h)
    with h.session_factory() as session:
        user_id = sign_up(session, h.clock, f"{uuid.uuid4().hex}@example.com", TEST_PASSWORD).user_id
        session.execute(
            text("UPDATE gateway.payments SET payer_user_id = :user_id WHERE id = :payment_id"),
            {"user_id": user_id, "payment_id": payment_id},
        )
        session.commit()
    return payment_id, user_id


def _dispute_events(session_factory: sessionmaker[Session]) -> list[DisputeEventV1]:
    with session_factory() as session:
        rows = session.execute(text(DISPUTE_ROWS), {"topic": DISPUTES_TOPIC}).scalars().all()
    return [DisputeEventV1.model_validate(row) for row in rows]


def _dispute_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.disputes")).scalar_one())


def test_the_gateway_api_amount_ceiling_is_the_contract_ceiling() -> None:
    # Arrange
    gateway_ceiling = MAX_AMOUNT_MINOR

    # Act
    contract_ceiling = DISPUTE_MAX_AMOUNT_MINOR

    # Assert
    assert gateway_ceiling == contract_ceiling


@pytest.mark.parametrize("role", ["merchant_api", "ops_agent", "merchant_admin", ""])
def test_opening_with_an_unknown_actor_role_raises_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, role: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    with pytest.raises(UnknownDisputeActorError):
        open_dispute(session_factory, clock, payment_id, "13.1", actor_role=role)

    # Assert
    assert (_dispute_count(session_factory), _dispute_events(session_factory)) == (0, [])


def test_a_consumer_open_without_a_payer_raises_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)

    # Act
    with pytest.raises(UnknownDisputeActorError):
        open_dispute(session_factory, clock, payment_id, "not me", actor_id="user:x", actor_role="consumer")

    # Assert
    assert (_dispute_count(session_factory), _dispute_events(session_factory)) == (0, [])


def test_a_consumer_open_by_the_payer_is_labelled_consumer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id, user_id = _paid_by_user(h)

    # Act
    open_dispute(
        session_factory,
        clock,
        payment_id,
        "not me",
        payer_user_id=user_id,
        actor_id=f"user:{user_id}",
        actor_role="consumer",
    )

    # Assert
    assert [(e.actor_role, e.network_reason_code) for e in _dispute_events(session_factory)] == [
        ("consumer", None)
    ]


def test_a_card_network_open_of_a_user_paid_payment_is_labelled_card_network(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id, _user_id = _paid_by_user(h)

    # Act
    open_dispute(session_factory, clock, payment_id, "13.1")

    # Assert
    assert [(e.actor_role, e.network_reason_code) for e in _dispute_events(session_factory)] == [
        ("card_network", "13.1")
    ]


@pytest.mark.parametrize("role", ["ops_agent", "merchant_admin", "consumer", ""])
def test_resolving_with_an_unknown_actor_role_raises_and_writes_no_resolved_event(
    session_factory: sessionmaker[Session], clock: FixedClock, role: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = open_dispute(session_factory, clock, _charge(h), "13.1")
    submit_evidence(session_factory, clock, h.merchant_id, view.dispute_id, EVIDENCE_TEXT)

    # Act
    with pytest.raises(UnknownDisputeActorError):
        resolve_dispute(session_factory, clock, view.dispute_id, "lost", "someone", role)

    # Assert
    assert [e.to_state for e in _dispute_events(session_factory)] == ["opened", "under_review"]
    with session_factory() as session:
        state = session.execute(
            text("SELECT state FROM gateway.disputes WHERE id = :id"), {"id": view.dispute_id}
        ).scalar_one()
    assert state == "under_review"


def test_a_network_shaped_reason_from_a_consumer_is_never_sent_as_a_reason_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id, user_id = _paid_by_user(h)
    view = open_dispute(
        session_factory,
        clock,
        payment_id,
        "13.1",
        payer_user_id=user_id,
        actor_id=f"user:{user_id}",
        actor_role="consumer",
    )

    # Act
    submit_evidence(session_factory, clock, h.merchant_id, view.dispute_id, EVIDENCE_TEXT)
    resolve_dispute(session_factory, clock, view.dispute_id, "won", "network", "card_network")

    # Assert
    events = _dispute_events(session_factory)
    assert [(e.to_state, e.actor_role, e.network_reason_code) for e in events] == [
        ("opened", "consumer", None),
        ("under_review", "merchant", None),
        ("won", "card_network", None),
    ]


def test_a_card_network_reason_code_follows_every_event_of_a_network_opened_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    view = open_dispute(session_factory, clock, _charge(h), "10.4")

    # Act
    submit_evidence(session_factory, clock, h.merchant_id, view.dispute_id, EVIDENCE_TEXT)
    resolve_dispute(session_factory, clock, view.dispute_id, "lost", "ops:admin", "ops_lead")

    # Assert
    assert [e.network_reason_code for e in _dispute_events(session_factory)] == ["10.4", "10.4", "10.4"]
