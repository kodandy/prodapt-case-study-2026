import json

from redis import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.outbox.models import OutboxEvent


def publish_pending(
    session_factory: sessionmaker[Session], redis: Redis, clock: Clock, batch_size: int = 100
) -> int:
    """XADDs unpublished outbox rows (FOR UPDATE SKIP LOCKED, id order) to the stream named by `topic`.

    Stream entry fields: the row's headers (e.g. `traceparent`, so consumers join the producing request's
    trace), then `event_id` and `payload` (JSON), which no header can override. Marks rows published; returns
    the count published.
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
            fields: dict[FieldT, EncodableT] = {name: value for name, value in row.headers.items()}
            fields["event_id"] = str(row.event_id)
            fields["payload"] = json.dumps(row.payload)
            redis.xadd(row.topic, fields)
            row.published_at = now
        return len(rows)
