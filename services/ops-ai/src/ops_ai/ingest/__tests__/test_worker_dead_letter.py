"""Tests that run_consumer survives a malformed stream entry, written from docs/specs/sq3.md H-1 (RT-002)."""

import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from ops_ai.ingest.consumer import PAYMENT_FACTS_GROUP
from ops_ai.ingest.worker import run_consumer
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

DEAD_TOPIC = "gw.payments.v1.dead"
WAIT_SECONDS = 15.0
BLOCK_MS = 50
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


class _Worker:
    """Runs run_consumer in a daemon thread so a wedged or crashed loop fails the test instead of hanging."""

    def __init__(self, target: Callable[[], int]) -> None:
        self._target = target
        self._result: int | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self._result = self._target()
        except BaseException as exc:
            self._error = exc

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def finish(self, stop: threading.Event) -> int:
        stop.set()
        self._thread.join(WAIT_SECONDS)
        if self._thread.is_alive():
            pytest.fail(f"run_consumer did not return within {WAIT_SECONDS}s after stop was set")
        if self._error is not None:
            raise self._error
        if self._result is None:
            pytest.fail("run_consumer returned without a result")
        return self._result


def _wait_until(condition: Callable[[], bool], worker: _Worker) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and worker.is_alive():
        if condition():
            return
        time.sleep(0.02)


def _event(n: int) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=n),
        event_type="payment.declined",
        occurred_at=OCCURRED_AT,
        entity_version=1,
        payment_id=uuid.UUID(int=1000 + n),
        merchant_id=uuid.UUID(int=7),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
    )


def _add_valid(redis: Redis, event: PaymentEventV1) -> None:
    redis.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})


def _fact_ids(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    with session_factory() as session:
        rows = session.execute(text("SELECT payment_id FROM ops_ai.payment_facts ORDER BY payment_id"))
        return list(rows.scalars())


def _pending(redis: Redis) -> int:
    return int(redis.xpending(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)["pending"])


def _dead_count(redis: Redis) -> int:
    rows = redis.xrange(DEAD_TOPIC, "-", "+")
    assert isinstance(rows, list)
    return len(rows)


def _run(session_factory: sessionmaker[Session], redis: Redis, stop: threading.Event) -> _Worker:
    return _Worker(
        lambda: run_consumer(
            session_factory, redis, _clock_now, "test-consumer-dead", stop, block_ms=BLOCK_MS
        )
    )


@pytest.mark.parametrize(
    "bad_fields",
    [
        {"event_id": "x", "payload": "not json at all"},
        {"event_id": "x"},
        {"event_id": "x", "payload": json.dumps({"event_id": "not-a-uuid"})},
    ],
    ids=["not_json", "missing_payload", "schema_invalid"],
)
def test_run_consumer_projects_a_good_entry_that_follows_a_malformed_one_and_does_not_wedge(
    session_factory: sessionmaker[Session], redis_client: Redis, bad_fields: dict[str, str]
) -> None:
    # Arrange
    good = _event(1)
    redis_client.xadd(PAYMENTS_TOPIC, {key: value for key, value in bad_fields.items()})
    _add_valid(redis_client, good)
    stop = threading.Event()
    worker = _run(session_factory, redis_client, stop)

    # Act
    worker.start()
    _wait_until(lambda: len(_fact_ids(session_factory)) == 1, worker)
    total = worker.finish(stop)

    # Assert
    assert _fact_ids(session_factory) == [good.payment_id]
    assert total == 2
    assert _dead_count(redis_client) == 1
    assert _pending(redis_client) == 0


def test_run_consumer_keeps_projecting_events_that_arrive_after_a_malformed_entry(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    first, second = _event(2), _event(3)
    _add_valid(redis_client, first)
    redis_client.xadd(PAYMENTS_TOPIC, {"event_id": "x", "payload": "{broken"})
    stop = threading.Event()
    worker = _run(session_factory, redis_client, stop)
    worker.start()
    _wait_until(lambda: len(_fact_ids(session_factory)) == 1 and _dead_count(redis_client) == 1, worker)

    # Act
    _add_valid(redis_client, second)
    _wait_until(lambda: len(_fact_ids(session_factory)) == 2, worker)
    total = worker.finish(stop)

    # Assert
    assert _fact_ids(session_factory) == sorted([first.payment_id, second.payment_id])
    assert total == 3
    assert _dead_count(redis_client) == 1
    assert _pending(redis_client) == 0


def test_run_consumer_dead_letters_a_malformed_entry_that_an_earlier_crashed_run_left_pending(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange: the bad entry is delivered to the same consumer name but never acknowledged
    redis_client.xadd(PAYMENTS_TOPIC, {"event_id": "x", "payload": "{broken"})
    good = _event(4)
    _add_valid(redis_client, good)
    redis_client.xgroup_create(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP, id="0", mkstream=True)
    redis_client.xreadgroup(PAYMENT_FACTS_GROUP, "test-consumer-dead", {PAYMENTS_TOPIC: ">"}, count=1)
    stop = threading.Event()
    worker = _run(session_factory, redis_client, stop)

    # Act
    worker.start()
    _wait_until(lambda: len(_fact_ids(session_factory)) == 1, worker)
    worker.finish(stop)

    # Assert
    assert _fact_ids(session_factory) == [good.payment_id]
    assert _dead_count(redis_client) == 1
    assert _pending(redis_client) == 0
