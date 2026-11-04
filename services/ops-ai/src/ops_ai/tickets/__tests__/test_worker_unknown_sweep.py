"""Tests for the unknown-payment sweep inside run_consumer, written from the B12 tickets spec (section 4c)."""

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from contracts.events import PaymentEventV1
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.ingest.worker import UNKNOWN_SWEEP_INTERVAL, run_consumer
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

WAIT_SECONDS = 15.0
BLOCK_MS = 50
QUIET_SECONDS = 0.6
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class _Worker:
    """Runs the blocking consumer in a daemon thread so a broken loop fails the test instead of hanging."""

    def __init__(self, target: Callable[[], int]) -> None:
        self._target = target
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            self._target()
        except BaseException as exc:
            self._error = exc

    def start(self) -> None:
        self._thread.start()

    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def finish(self, stop: threading.Event) -> None:
        stop.set()
        self._thread.join(WAIT_SECONDS)
        if self._thread.is_alive():
            pytest.fail(f"run_consumer did not return within {WAIT_SECONDS}s after stop was set")
        if self._error is not None:
            raise self._error


def _unknown_event(n: int) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=900_000 + n),
        event_type="payment.unknown",
        occurred_at=TEST_NOW - timedelta(hours=1, minutes=n),
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=MERCHANT,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="unknown",
        provider="acq_a",
        decline_code=None,
        card_bin="400000",
        card_last4="0002",
    )


def _deliver(session_factory: sessionmaker[Session], event: PaymentEventV1) -> None:
    with session_factory.begin() as session:
        apply_payment_event(session, lambda: TEST_NOW, event)


def _ticket_keys(session_factory: sessionmaker[Session]) -> list[str]:
    with session_factory() as session:
        return list(
            session.execute(text("SELECT source_key FROM ops_ai.tickets ORDER BY source_key")).scalars()
        )


def _wait_until(condition: Callable[[], bool], worker: _Worker) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and worker.is_alive():
        if condition():
            return
        time.sleep(0.02)


def _start(
    session_factory: sessionmaker[Session], redis: Redis, clock: MutableClock, stop: threading.Event
) -> _Worker:
    worker = _Worker(
        lambda: run_consumer(session_factory, redis, clock, "test-consumer", stop, block_ms=BLOCK_MS)
    )
    worker.start()
    return worker


def test_the_sweep_interval_is_sixty_seconds() -> None:
    # Arrange / Act / Assert
    assert UNKNOWN_SWEEP_INTERVAL == timedelta(seconds=60)


def test_run_consumer_sweeps_unknown_payments_on_its_first_iteration(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _unknown_event(1)
    _deliver(session_factory, event)
    stop = threading.Event()

    # Act
    worker = _start(session_factory, redis_client, MutableClock(TEST_NOW), stop)
    _wait_until(lambda: len(_ticket_keys(session_factory)) == 1, worker)
    worker.finish(stop)

    # Assert
    assert _ticket_keys(session_factory) == [f"unknown_payment:{event.payment_id}"]


def test_run_consumer_does_not_sweep_again_before_the_interval_has_passed_on_the_injected_clock(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    first = _unknown_event(1)
    second = _unknown_event(2)
    _deliver(session_factory, first)
    clock = MutableClock(TEST_NOW)
    stop = threading.Event()
    worker = _start(session_factory, redis_client, clock, stop)
    _wait_until(lambda: len(_ticket_keys(session_factory)) == 1, worker)
    _deliver(session_factory, second)

    # Act
    clock.now = TEST_NOW + timedelta(seconds=30)
    time.sleep(QUIET_SECONDS)
    keys_inside_interval = _ticket_keys(session_factory)
    worker.finish(stop)

    # Assert
    assert keys_inside_interval == [f"unknown_payment:{first.payment_id}"]


def test_run_consumer_sweeps_again_once_the_injected_clock_passes_the_interval(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    first = _unknown_event(1)
    second = _unknown_event(2)
    _deliver(session_factory, first)
    clock = MutableClock(TEST_NOW)
    stop = threading.Event()
    worker = _start(session_factory, redis_client, clock, stop)
    _wait_until(lambda: len(_ticket_keys(session_factory)) == 1, worker)
    _deliver(session_factory, second)

    # Act
    clock.now = TEST_NOW + timedelta(seconds=61)
    _wait_until(lambda: len(_ticket_keys(session_factory)) == 2, worker)
    worker.finish(stop)

    # Assert
    assert _ticket_keys(session_factory) == sorted(
        [f"unknown_payment:{first.payment_id}", f"unknown_payment:{second.payment_id}"]
    )


def test_run_consumer_leaves_a_payment_inside_the_grace_window_without_a_ticket(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    fresh = _unknown_event(1).model_copy(update={"occurred_at": TEST_NOW - timedelta(minutes=1)})
    _deliver(session_factory, fresh)
    stop = threading.Event()

    # Act
    worker = _start(session_factory, redis_client, MutableClock(TEST_NOW), stop)
    time.sleep(QUIET_SECONDS)
    worker.finish(stop)

    # Assert
    assert _ticket_keys(session_factory) == []
