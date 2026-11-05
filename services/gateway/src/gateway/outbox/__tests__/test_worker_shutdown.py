"""Graceful shutdown (P1-6): a stop set mid-batch lets run_relay finish that batch before it returns."""

import json
import threading
import uuid
from collections import Counter
from datetime import datetime
from typing import cast

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.clock import FixedClock
from gateway.outbox.models import OutboxEvent
from gateway.outbox.worker import run_relay
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

StreamEntries = list[tuple[str, dict[str, str]]]


class _StopOnFirstCall:
    """A clock that sets `stop` when first called.

    publish_pending reads the clock after selecting its batch and before the XADD loop, so the stop lands
    mid-batch, deterministically and without threads.
    """

    def __init__(self, base: FixedClock, stop: threading.Event) -> None:
        self._base = base
        self._stop = stop
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        self._stop.set()
        return self._base()


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


def _unpublished_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        count = session.execute(
            text("SELECT count(*) FROM gateway.outbox WHERE published_at IS NULL")
        ).scalar_one()
    return int(count)


def _stream_event_ids(redis: Redis) -> list[str]:
    return [fields["event_id"] for _, fields in cast(StreamEntries, redis.xrange(PAYMENTS_TOPIC))]


def test_run_relay_finishes_the_in_flight_batch_when_stop_is_set_mid_batch(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    events = [_event(clock) for _ in range(5)]
    _enqueue(session_factory, clock, events)
    stop = threading.Event()
    stopping_clock = _StopOnFirstCall(clock, stop)

    # Act
    total = run_relay(session_factory, redis_client, stopping_clock, stop, poll_interval_seconds=0.05)

    # Assert
    assert total == 5
    assert stopping_clock.calls == 1
    assert _unpublished_count(session_factory) == 0
    assert Counter(_stream_event_ids(redis_client)) == Counter(str(event.event_id) for event in events)


def test_run_relay_stamps_published_at_on_every_row_of_the_in_flight_batch(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    events = [_event(clock) for _ in range(3)]
    _enqueue(session_factory, clock, events)
    stop = threading.Event()

    # Act
    run_relay(session_factory, redis_client, _StopOnFirstCall(clock, stop), stop, poll_interval_seconds=0.05)

    # Assert
    with session_factory() as session:
        published_at = (
            session.execute(text("SELECT published_at FROM gateway.outbox ORDER BY id")).scalars().all()
        )
    assert published_at == [clock(), clock(), clock()]


def test_run_relay_publishes_each_event_exactly_once_across_a_second_run(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    events = [_event(clock) for _ in range(4)]
    _enqueue(session_factory, clock, events)
    first_stop = threading.Event()
    first_clock = _StopOnFirstCall(clock, first_stop)
    run_relay(session_factory, redis_client, first_clock, first_stop, poll_interval_seconds=0.05)
    second_stop = threading.Event()

    # Act
    second_total = run_relay(
        session_factory,
        redis_client,
        _StopOnFirstCall(clock, second_stop),
        second_stop,
        poll_interval_seconds=0.05,
    )

    # Assert
    assert second_total == 0
    assert redis_client.xlen(PAYMENTS_TOPIC) == 4
    assert Counter(_stream_event_ids(redis_client)) == Counter(str(event.event_id) for event in events)


def test_run_relay_with_stop_set_mid_iteration_and_an_empty_outbox_returns_zero(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    stop = threading.Event()
    stopping_clock = _StopOnFirstCall(clock, stop)

    # Act
    total = run_relay(session_factory, redis_client, stopping_clock, stop, poll_interval_seconds=60.0)

    # Assert
    assert total == 0
    assert stopping_clock.calls == 1
    assert redis_client.exists(PAYMENTS_TOPIC) == 0
