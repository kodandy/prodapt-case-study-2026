"""The ops-ai consumer worker also drains gw.disputes.v1 (R2-1 / R2-2)."""

import threading
import time
import uuid
from datetime import UTC, datetime

from contracts.events import DISPUTES_TOPIC, DisputeEventV1
from ops_ai.ingest.disputes import DISPUTE_WORKFLOWS_GROUP
from ops_ai.ingest.worker import run_consumer
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

WAIT_SECONDS = 15.0
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


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
            "merchant_id": uuid.uuid4(),
            "from_state": None,
            "to_state": "opened",
            "actor_role": "card_network",
            "network_reason_code": "10.4",
            "currency": "USD",
            "payment_amount_minor": 1500,
            "refunded_minor": 0,
            "dispute_amount_minor": 1500,
            "chargeback_minor": 0,
            "chargeback_fee_minor": 0,
        }
    )


def _projected(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    with session_factory() as session:
        return list(session.execute(text("SELECT dispute_id FROM ops_ai.dispute_workflows")).scalars())


def test_run_consumer_projects_a_published_dispute_event_and_acks_it(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    event = _opened(dispute_id)
    redis_client.xadd(DISPUTES_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})
    stop = threading.Event()
    thread = threading.Thread(
        target=run_consumer,
        args=(session_factory, redis_client, _now, "test-consumer", stop, 50),
        daemon=True,
    )

    # Act
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and not _projected(session_factory):
        time.sleep(0.02)
    stop.set()
    thread.join(WAIT_SECONDS)

    # Assert
    assert thread.is_alive() is False
    assert _projected(session_factory) == [dispute_id]
    assert redis_client.xpending(DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP)["pending"] == 0
