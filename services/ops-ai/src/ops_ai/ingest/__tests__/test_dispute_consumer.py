"""Tests for ops_ai.ingest.disputes: gw.disputes.v1 projected into ops_ai.dispute_workflows (R2-1 / R2-2)."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from contracts.events import DISPUTES_TOPIC, DisputeEventV1
from ops_ai.ingest.disputes import (
    DISPUTE_WORKFLOWS_GROUP,
    DISPUTES_DEAD_LETTER_TOPIC,
    apply_dispute_event,
    consume_disputes,
)
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

OPENED_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
WORKFLOWS = (
    "SELECT dispute_id, merchant_id, payment_id, network_reason_code, currency, payment_amount_minor, "
    "refunded_minor, dispute_amount_minor, chargeback_minor, chargeback_fee_minor, entity_version, "
    "updated_at FROM ops_ai.dispute_workflows ORDER BY dispute_id"
)
STEPS = (
    "SELECT entity_version, event_type, from_state, to_state, actor_role, occurred_at "
    "FROM ops_ai.dispute_workflow_steps WHERE dispute_id = :dispute_id ORDER BY entity_version"
)


def _now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _lifecycle(
    dispute_id: uuid.UUID, merchant_id: uuid.UUID = MERCHANT, outcome: str = "lost"
) -> list[DisputeEventV1]:
    payment_id = uuid.UUID(int=dispute_id.int ^ 1)
    shared = {
        "dispute_id": dispute_id,
        "payment_id": payment_id,
        "merchant_id": merchant_id,
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 1500,
        "refunded_minor": 0,
        "dispute_amount_minor": 1500,
    }
    steps = [
        ("dispute.opened", None, "opened", "card_network", 0, 0),
        ("dispute.evidence_submitted", "opened", "under_review", "merchant", 0, 0),
        (
            "dispute.resolved",
            "under_review",
            outcome,
            "ops_lead",
            *((1500, 1500) if outcome == "lost" else (0, 0)),
        ),
    ]
    return [
        DisputeEventV1.model_validate(
            {
                **shared,
                "event_id": uuid.uuid4(),
                "event_type": event_type,
                "occurred_at": OPENED_AT + timedelta(days=index),
                "entity_version": index + 1,
                "from_state": from_state,
                "to_state": to_state,
                "actor_role": actor_role,
                "chargeback_minor": chargeback,
                "chargeback_fee_minor": fee,
            }
        )
        for index, (event_type, from_state, to_state, actor_role, chargeback, fee) in enumerate(steps)
    ]


def _apply(session_factory: sessionmaker[Session], event: DisputeEventV1) -> bool:
    with session_factory.begin() as session:
        return apply_dispute_event(session, _now, event)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def test_a_full_workflow_projects_one_row_and_three_ordered_steps(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    events = _lifecycle(dispute_id)

    # Act
    applied = [_apply(session_factory, event) for event in events]

    # Assert
    assert applied == [True, True, True]
    workflows = _rows(session_factory, WORKFLOWS)
    assert workflows == [
        {
            "dispute_id": dispute_id,
            "merchant_id": MERCHANT,
            "payment_id": events[0].payment_id,
            "network_reason_code": "13.1",
            "currency": "USD",
            "payment_amount_minor": 1500,
            "refunded_minor": 0,
            "dispute_amount_minor": 1500,
            "chargeback_minor": 1500,
            "chargeback_fee_minor": 1500,
            "entity_version": 3,
            "updated_at": OPENED_AT + timedelta(days=2),
        }
    ]
    steps = _rows(session_factory, STEPS, dispute_id=dispute_id)
    assert [(s["entity_version"], s["from_state"], s["to_state"], s["actor_role"]) for s in steps] == [
        (1, None, "opened", "card_network"),
        (2, "opened", "under_review", "merchant"),
        (3, "under_review", "lost", "ops_lead"),
    ]


def test_a_redelivered_event_is_a_duplicate_and_changes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    opened = _lifecycle(dispute_id)[0]
    _apply(session_factory, opened)
    before = (_rows(session_factory, WORKFLOWS), _rows(session_factory, STEPS, dispute_id=dispute_id))

    # Act
    applied = _apply(session_factory, opened)

    # Assert
    assert applied is False
    assert (_rows(session_factory, WORKFLOWS), _rows(session_factory, STEPS, dispute_id=dispute_id)) == before
    processed = _rows(
        session_factory,
        "SELECT event_id FROM ops_ai.processed_events WHERE consumer = :consumer",
        consumer=DISPUTE_WORKFLOWS_GROUP,
    )
    assert processed == [{"event_id": opened.event_id}]


def test_out_of_order_delivery_keeps_the_highest_version_and_every_step(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    opened, evidence, resolved = _lifecycle(dispute_id, outcome="won")

    # Act
    for event in (resolved, opened, evidence):
        _apply(session_factory, event)

    # Assert
    workflow = _rows(session_factory, WORKFLOWS)[0]
    assert (workflow["entity_version"], workflow["updated_at"]) == (3, resolved.occurred_at)
    steps = _rows(session_factory, STEPS, dispute_id=dispute_id)
    assert [s["to_state"] for s in steps] == ["opened", "under_review", "won"]


def test_an_event_moving_a_dispute_to_another_merchant_is_consumed_and_dropped(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    opened = _lifecycle(dispute_id)[0]
    hijack = _lifecycle(dispute_id, merchant_id=OTHER_MERCHANT)[1]
    _apply(session_factory, opened)

    # Act
    applied = _apply(session_factory, hijack)

    # Assert
    assert applied is True
    assert [row["merchant_id"] for row in _rows(session_factory, WORKFLOWS)] == [MERCHANT]
    assert [s["entity_version"] for s in _rows(session_factory, STEPS, dispute_id=dispute_id)] == [1]


def test_consume_disputes_projects_stream_entries_and_acks_them(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    for event in _lifecycle(dispute_id):
        redis_client.xadd(
            DISPUTES_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()}
        )

    # Act
    read = consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    assert read == 3
    assert [row["entity_version"] for row in _rows(session_factory, WORKFLOWS)] == [3]
    pending = redis_client.xpending(DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP)
    assert pending["pending"] == 0


def test_a_malformed_entry_is_dead_lettered_and_acked_without_touching_the_projection(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    bad = _lifecycle(uuid.uuid4())[0].model_dump(mode="json") | {"evidence_text": "Jane Roe, 1 Main St"}
    redis_client.xadd(DISPUTES_TOPIC, {"payload": json.dumps(bad)})

    # Act
    read = consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    assert read == 1
    assert _rows(session_factory, WORKFLOWS) == []
    dead = redis_client.xrange(DISPUTES_DEAD_LETTER_TOPIC)
    assert isinstance(dead, list)
    assert [(fields or {}).get("error") for _id, fields in dead] == ["ValidationError"]
    assert redis_client.xpending(DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP)["pending"] == 0
