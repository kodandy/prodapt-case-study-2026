from contracts.events import PAYMENTS_TOPIC
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapper, Session, sessionmaker

from gateway.clock import Clock
from gateway.observability.metrics import OUTBOX_OLDEST_AGE, OUTBOX_PENDING, PAYMENT_EVENTS
from gateway.observability.tracing import TRACEPARENT, current_traceparent
from gateway.outbox.models import OutboxEvent


def _before_outbox_insert(_mapper: Mapper[OutboxEvent], _connection: Connection, row: OutboxEvent) -> None:
    """Carries the active trace into the row's headers (the relay copies headers onto the stream entry) and
    counts payment events. Counted at insert, so a rolled-back transaction can over-count by its rows."""
    traceparent = current_traceparent()
    if traceparent is not None and TRACEPARENT not in row.headers:
        row.headers = {**row.headers, TRACEPARENT: traceparent}
    if row.topic == PAYMENTS_TOPIC:
        payload = row.payload
        PAYMENT_EVENTS.labels(
            str(payload.get("method")), str(payload.get("state")), str(payload.get("decline_code") or "none")
        ).inc()


def register_outbox_hooks() -> None:
    if not event.contains(OutboxEvent, "before_insert", _before_outbox_insert):
        event.listen(OutboxEvent, "before_insert", _before_outbox_insert)


def record_outbox_backlog(session_factory: sessionmaker[Session], clock: Clock) -> tuple[int, float]:
    """(unpublished rows, age in seconds of the oldest) and the matching gauges, for the outbox-lag alert."""
    with session_factory() as session:
        pending, oldest = session.execute(
            select(func.count(), func.min(OutboxEvent.created_at)).where(OutboxEvent.published_at.is_(None))
        ).one()
    age = 0.0 if oldest is None else max((clock() - oldest).total_seconds(), 0.0)
    OUTBOX_PENDING.set(pending)
    OUTBOX_OLDEST_AGE.set(age)
    return int(pending), age
