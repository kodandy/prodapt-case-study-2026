"""Tests for gateway/outbox/worker.py, written from docs/specs/runtime.md (R3 run_relay and main)."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.clock import FixedClock
from gateway.outbox.models import OutboxEvent
from gateway.outbox.worker import run_relay
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

StreamEntries = list[tuple[str, dict[str, str]]]

WAIT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.05
REDIS_CANARY = "canary-redis-detail-7f2c"
DB_CANARY = "canary-db-detail-4d9e"


class _Worker:
    """Runs a blocking worker in a daemon thread so a broken loop fails the test instead of hanging."""

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
            pytest.fail(f"run_relay did not return within {WAIT_SECONDS}s after stop was set")
        if self._error is not None:
            raise self._error
        if self._result is None:
            pytest.fail("run_relay returned without a result")
        return self._result


def _wait_until(condition: Callable[[], bool], worker: _Worker) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and worker.is_alive():
        if condition():
            return
        time.sleep(0.02)


@dataclass
class _Flaky:
    """Fails the first `failures` calls with `error`, then delegates to the original callable."""

    original: Callable[..., object]
    error: Exception
    failures: int
    calls: int = 0

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return self.original(*args, **kwargs)


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


def _published_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        count = session.execute(
            text("SELECT count(*) FROM gateway.outbox WHERE published_at IS NOT NULL")
        ).scalar_one()
    return int(count)


def _stream_event_ids(redis: Redis) -> list[str]:
    return [fields["event_id"] for _, fields in cast(StreamEntries, redis.xrange(PAYMENTS_TOPIC))]


def test_run_relay_publishes_the_pending_row_and_returns_the_total_when_stopped(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    event = _event(clock)
    _enqueue(session_factory, clock, [event])
    stop = threading.Event()
    worker = _Worker(
        lambda: run_relay(
            session_factory, redis_client, clock, stop, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
    )

    # Act
    worker.start()
    _wait_until(lambda: _published_count(session_factory) == 1, worker)
    total = worker.finish(stop)

    # Assert
    assert total == 1
    assert _stream_event_ids(redis_client) == [str(event.event_id)]
    assert _published_count(session_factory) == 1


def test_run_relay_keeps_polling_and_totals_rows_that_arrive_after_it_started(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    first_wave = [_event(clock), _event(clock)]
    second_wave = [_event(clock)]
    stop = threading.Event()
    worker = _Worker(
        lambda: run_relay(
            session_factory, redis_client, clock, stop, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
    )
    worker.start()
    time.sleep(0.3)

    # Act
    _enqueue(session_factory, clock, first_wave)
    _wait_until(lambda: _published_count(session_factory) == 2, worker)
    _enqueue(session_factory, clock, second_wave)
    _wait_until(lambda: _published_count(session_factory) == 3, worker)
    total = worker.finish(stop)

    # Assert
    assert total == 3
    assert _stream_event_ids(redis_client) == [str(event.event_id) for event in [*first_wave, *second_wave]]


def test_run_relay_with_the_stop_event_already_set_returns_zero_and_publishes_nothing(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    stop = threading.Event()
    worker = _Worker(
        lambda: run_relay(
            session_factory, redis_client, clock, stop, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
    )

    # Act
    stop.set()
    worker.start()
    total = worker.finish(stop)

    # Assert
    assert total == 0
    assert redis_client.exists(PAYMENTS_TOPIC) == 0


def test_run_relay_stops_promptly_even_with_a_long_poll_interval(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    stop = threading.Event()
    worker = _Worker(
        lambda: run_relay(session_factory, redis_client, clock, stop, poll_interval_seconds=60.0)
    )
    worker.start()
    time.sleep(0.3)

    # Act
    started = time.monotonic()
    total = worker.finish(stop)
    elapsed = time.monotonic() - started

    # Assert
    assert total == 0
    assert elapsed < 5.0


def test_run_relay_survives_a_redis_error_logs_only_the_class_name_and_publishes_on_retry(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)
    event = _event(clock)
    _enqueue(session_factory, clock, [event])
    flaky = _Flaky(original=redis_client.xadd, error=RedisConnectionError(REDIS_CANARY), failures=2)
    monkeypatch.setattr(redis_client, "xadd", flaky)
    stop = threading.Event()
    worker = _Worker(
        lambda: run_relay(
            session_factory, redis_client, clock, stop, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
    )

    # Act
    worker.start()
    _wait_until(lambda: _published_count(session_factory) == 1, worker)
    total = worker.finish(stop)

    # Assert
    captured = capsys.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert total == 1
    assert flaky.calls == 3
    assert _stream_event_ids(redis_client) == [str(event.event_id)]
    assert "ConnectionError" in logged
    assert REDIS_CANARY not in logged


def test_run_relay_survives_a_database_error_logs_only_the_class_name_and_publishes_on_retry(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)
    event = _event(clock)
    _enqueue(session_factory, clock, [event])
    error = OperationalError("SELECT 1", {}, Exception(DB_CANARY))
    flaky = _Flaky(original=session_factory.begin, error=error, failures=2)
    monkeypatch.setattr(session_factory, "begin", flaky)
    stop = threading.Event()
    worker = _Worker(
        lambda: run_relay(
            session_factory, redis_client, clock, stop, poll_interval_seconds=POLL_INTERVAL_SECONDS
        )
    )

    # Act
    worker.start()
    _wait_until(lambda: _published_count(session_factory) == 1, worker)
    total = worker.finish(stop)

    # Assert
    captured = capsys.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert total == 1
    assert flaky.calls >= 3
    assert _stream_event_ids(redis_client) == [str(event.event_id)]
    assert "OperationalError" in logged
    assert DB_CANARY not in logged


def test_run_relay_sleeps_between_failing_iterations_instead_of_spinning(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    _enqueue(session_factory, clock, [_event(clock)])
    always_failing = _Flaky(
        original=redis_client.xadd, error=RedisConnectionError(REDIS_CANARY), failures=10**9
    )
    monkeypatch.setattr(redis_client, "xadd", always_failing)
    stop = threading.Event()
    worker = _Worker(lambda: run_relay(session_factory, redis_client, clock, stop, poll_interval_seconds=0.1))

    # Act
    started = time.monotonic()
    worker.start()
    _wait_until(lambda: always_failing.calls >= 3, worker)
    elapsed = time.monotonic() - started
    total = worker.finish(stop)

    # Assert
    assert always_failing.calls >= 3
    assert elapsed >= 0.1
    assert total == 0


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_main_publishes_pending_rows_and_exits_zero_on_the_stop_signal(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    clock: FixedClock,
    stop_signal: signal.Signals,
) -> None:
    # Arrange
    event = _event(clock)
    _enqueue(session_factory, clock, [event])
    env = {
        **os.environ,
        "GATEWAY_API_KEY_PEPPER": "p" * 40,
        "GATEWAY_CARD_FINGERPRINT_KEY": "f" * 40,
    }
    env.pop("GATEWAY_SECRETS_DIR", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "gateway.outbox.worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Act
    try:
        deadline = time.monotonic() + WAIT_SECONDS + 10
        while time.monotonic() < deadline and process.poll() is None:
            if _published_count(session_factory) == 1:
                break
            time.sleep(0.1)
        published = _published_count(session_factory)
        process.send_signal(stop_signal)
        _, stderr = process.communicate(timeout=WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    # Assert
    assert published == 1
    assert _stream_event_ids(redis_client) == [str(event.event_id)]
    assert process.returncode == 0
    assert "Traceback" not in stderr
