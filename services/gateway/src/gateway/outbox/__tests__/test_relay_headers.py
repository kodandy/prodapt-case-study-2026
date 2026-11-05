"""publish_pending forwards stored outbox headers (e.g. `traceparent`) as stream entry fields."""

import json
import uuid
from typing import cast

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.clock import FixedClock
from gateway.outbox.models import OutboxEvent
from gateway.outbox.relay import publish_pending
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

StreamEntries = list[tuple[str, dict[str, str]]]

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def _event(clock: FixedClock) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.succeeded",
        occurred_at=clock(),
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="succeeded",
        provider="acq_a",
        decline_code=None,
        card_bin="424242",
        card_last4="4242",
    )


def _enqueue(
    session_factory: sessionmaker[Session], clock: FixedClock, event: PaymentEventV1, headers: dict[str, str]
) -> None:
    with session_factory() as session:
        session.add(
            OutboxEvent(
                event_id=event.event_id,
                topic=PAYMENTS_TOPIC,
                payload=json.loads(event.model_dump_json()),
                headers=headers,
                created_at=clock(),
                published_at=None,
            )
        )
        session.commit()


def _entries(redis: Redis) -> StreamEntries:
    return cast(StreamEntries, redis.xrange(PAYMENTS_TOPIC))


def test_publish_pending_copies_the_traceparent_header_into_the_stream_entry(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    event = _event(clock)
    _enqueue(session_factory, clock, event, {"traceparent": TRACEPARENT})

    # Act
    published = publish_pending(session_factory, redis_client, clock)

    # Assert
    entries = _entries(redis_client)
    assert published == 1
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["traceparent"] == TRACEPARENT
    assert set(fields) == {"traceparent", "event_id", "payload"}
    assert fields["event_id"] == str(event.event_id)
    assert PaymentEventV1.model_validate_json(fields["payload"]) == event


def test_publish_pending_without_headers_adds_only_event_id_and_payload(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _enqueue(session_factory, clock, _event(clock), {})

    # Act
    publish_pending(session_factory, redis_client, clock)

    # Assert
    fields = _entries(redis_client)[0][1]
    assert set(fields) == {"event_id", "payload"}


def test_publish_pending_forwards_every_stored_header(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    _enqueue(session_factory, clock, _event(clock), {"traceparent": TRACEPARENT, "x-source": "gateway"})

    # Act
    publish_pending(session_factory, redis_client, clock)

    # Assert
    fields = _entries(redis_client)[0][1]
    assert (fields["traceparent"], fields["x-source"]) == (TRACEPARENT, "gateway")


def test_publish_pending_never_lets_a_header_override_event_id_or_payload(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    event = _event(clock)
    _enqueue(session_factory, clock, event, {"event_id": "spoofed", "payload": "spoofed"})

    # Act
    publish_pending(session_factory, redis_client, clock)

    # Assert
    fields = _entries(redis_client)[0][1]
    assert fields["event_id"] == str(event.event_id)
    assert PaymentEventV1.model_validate_json(fields["payload"]) == event
