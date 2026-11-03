import json

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.outbox.models import OutboxEvent


def publish_pending(
    session_factory: sessionmaker[Session], redis: Redis, clock: Clock, batch_size: int = 100
) -> int:
    """XADDs unpublished outbox rows (FOR UPDATE SKIP LOCKED, id order) to the stream named by `topic`.

    Stream entry fields: `event_id`, `payload` (JSON). Marks rows published; returns the count published.
    Delivery is at least once: a crash after XADD and before commit re-sends the row.
    """
    with session_factory.begin() as session:
        rows = session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        ).all()
        now = clock()
        for row in rows:
            redis.xadd(row.topic, {"event_id": str(row.event_id), "payload": json.dumps(row.payload)})
            row.published_at = now
        return len(rows)
