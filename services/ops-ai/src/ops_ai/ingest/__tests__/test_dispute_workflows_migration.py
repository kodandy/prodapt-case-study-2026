"""Downgrading the dispute_workflows migration also forgets which dispute events were applied, so a replay
after re-upgrading rebuilds the projection (security audit L-5, red team H4)."""

import uuid
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from contracts.events import DisputeEventV1
from ops_ai.ingest.disputes import DISPUTE_WORKFLOWS_GROUP, apply_dispute_event
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

BEFORE_DISPUTE_WORKFLOWS = "e5a9c3f17b28"
OTHER_CONSUMER = "ops-ai.payment-facts"
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000021")


def _now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _opened(dispute_id: uuid.UUID) -> DisputeEventV1:
    return DisputeEventV1.model_validate(
        {
            "event_id": uuid.uuid4(),
            "event_type": "dispute.opened",
            "occurred_at": OCCURRED_AT,
            "entity_version": 1,
            "dispute_id": dispute_id,
            "payment_id": uuid.uuid4(),
            "merchant_id": MERCHANT,
            "from_state": None,
            "to_state": "opened",
            "actor_role": "card_network",
            "network_reason_code": "13.1",
            "currency": "USD",
            "payment_amount_minor": 1500,
            "refunded_minor": 0,
            "dispute_amount_minor": 1500,
            "chargeback_minor": 0,
            "chargeback_fee_minor": 0,
        }
    )


def _processed_by_consumer(session_factory: sessionmaker[Session]) -> dict[str, int]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT consumer, count(*) AS n FROM ops_ai.processed_events"
                " GROUP BY consumer ORDER BY consumer"
            )
        )
        return {row.consumer: int(row.n) for row in rows}


def _projected(session_factory: sessionmaker[Session]) -> list[tuple[uuid.UUID, int]]:
    with session_factory() as session:
        rows = session.execute(text("SELECT dispute_id, entity_version FROM ops_ai.dispute_workflows"))
        return [(row.dispute_id, row.entity_version) for row in rows]


def test_downgrade_forgets_applied_dispute_events_so_a_replay_rebuilds_the_projection(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    event = _opened(dispute_id)
    with session_factory.begin() as session:
        apply_dispute_event(session, _now, event)
        session.execute(
            text(
                "INSERT INTO ops_ai.processed_events (consumer, event_id, processed_at) "
                "VALUES (:consumer, :event_id, :processed_at)"
            ),
            {"consumer": OTHER_CONSUMER, "event_id": uuid.uuid4(), "processed_at": _now()},
        )
    config = Config(str(ALEMBIC_CONFIGS[1]))

    # Act
    try:
        command.downgrade(config, BEFORE_DISPUTE_WORKFLOWS)
        after_downgrade = _processed_by_consumer(session_factory)
    finally:
        command.upgrade(config, "head")
    with session_factory.begin() as session:
        replayed = apply_dispute_event(session, _now, event)

    # Assert
    assert after_downgrade == {OTHER_CONSUMER: 1}
    assert replayed is True
    assert _projected(session_factory) == [(dispute_id, 1)]
    assert _processed_by_consumer(session_factory) == {DISPUTE_WORKFLOWS_GROUP: 1, OTHER_CONSUMER: 1}
