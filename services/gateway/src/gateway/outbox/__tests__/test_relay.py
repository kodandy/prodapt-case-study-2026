import json
import uuid
from datetime import timedelta
from typing import cast

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.clock import FixedClock
from gateway.outbox.models import OutboxEvent
from gateway.outbox.relay import publish_pending
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

StreamEntries = list[tuple[str, dict[str, str]]]


def _event(clock: FixedClock, state: str = "succeeded") -> PaymentEventV1:
    declined = state == "declined"
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=clock(),
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="424242",
        card_last4="4242",
    )


def _enqueue(session_factory: sessionmaker[Session], clock: FixedClock, events: list[PaymentEventV1]) -> None:
    with session_factory() as session:
        for event in events:
            session.add(
                OutboxEvent(
                    event_id=event.event_id,
                    topic=PAYMENTS_TOPIC,
                    payload=json.loads(event.model_dump_json()),
                    headers={},
                    created_at=clock(),
                    published_at=None,
                )
            )
            session.flush()
        session.commit()


def _outbox_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        result = session.execute(text("SELECT event_id, published_at FROM gateway.outbox ORDER BY id"))
        return [dict(row) for row in result.mappings()]


def test_publish_pending_xadds_the_event_and_marks_the_row_published(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    event = _event(clock)
    _enqueue(session_factory, clock, [event])
    clock.advance(timedelta(minutes=1))

    # Act
    published = publish_pending(session_factory, redis_client, clock)

    # Assert
    entries = cast(StreamEntries, redis_client.xrange(PAYMENTS_TOPIC))
    assert published == 1
    assert len(entries) == 1
    fields = entries[0][1]
    assert set(fields) == {"event_id", "payload"}
    assert fields["event_id"] == str(event.event_id)
    assert PaymentEventV1.model_validate_json(fields["payload"]) == event
    assert _outbox_rows(session_factory) == [{"event_id": event.event_id, "published_at": clock()}]


def test_publish_pending_returns_zero_on_a_second_call_and_adds_nothing(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _enqueue(session_factory, clock, [_event(clock)])
    publish_pending(session_factory, redis_client, clock)

    # Act
    second = publish_pending(session_factory, redis_client, clock)

    # Assert
    assert second == 0
    assert redis_client.xlen(PAYMENTS_TOPIC) == 1


def test_publish_pending_with_an_empty_outbox_returns_zero_and_creates_no_stream(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange: no outbox rows

    # Act
    published = publish_pending(session_factory, redis_client, clock)

    # Assert
    assert published == 0
    assert redis_client.exists(PAYMENTS_TOPIC) == 0


def test_publish_pending_honours_batch_size_and_publishes_in_outbox_order(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    events = [_event(clock), _event(clock, state="declined"), _event(clock)]
    _enqueue(session_factory, clock, events)

    # Act
    counts = [publish_pending(session_factory, redis_client, clock, batch_size=2) for _ in range(3)]

    # Assert
    stream_event_ids = [
        fields["event_id"] for _, fields in cast(StreamEntries, redis_client.xrange(PAYMENTS_TOPIC))
    ]
    assert counts == [2, 1, 0]
    assert stream_event_ids == [str(event.event_id) for event in events]
