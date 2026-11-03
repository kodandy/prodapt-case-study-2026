"""Tests for ops_ai/ingest/worker.py, written from docs/specs/runtime.md (R6 run_consumer and main)."""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from ops_ai.ingest.consumer import PAYMENT_FACTS_GROUP
from ops_ai.ingest.worker import run_consumer
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

WAIT_SECONDS = 15.0
BLOCK_MS = 50
REDIS_CANARY = "canary-redis-detail-7f2c"
DB_CANARY = "canary-db-detail-4d9e"
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


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


def _event(*, declined: bool = True) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=OCCURRED_AT,
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0002" if declined else "4242",
    )


def _stream_add(redis: Redis, event: PaymentEventV1) -> None:
    redis.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})


def _fact_ids(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    with session_factory() as session:
        return list(session.execute(text("SELECT payment_id FROM ops_ai.payment_facts")).scalars())


def _pending(redis: Redis) -> int:
    return int(redis.xpending(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)["pending"])


def _consumer_names(redis: Redis) -> list[str]:
    try:
        return [
            str(consumer["name"]) for consumer in redis.xinfo_consumers(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
        ]
    except ResponseError:
        return []


def _run_consumer_in_worker(
    session_factory: sessionmaker[Session], redis: Redis, stop: threading.Event
) -> _Worker:
    return _Worker(
        lambda: run_consumer(session_factory, redis, _clock_now, "test-consumer", stop, block_ms=BLOCK_MS)
    )


def test_run_consumer_projects_a_published_event_and_returns_the_total_read_when_stopped(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _event()
    _stream_add(redis_client, event)
    stop = threading.Event()
    worker = _run_consumer_in_worker(session_factory, redis_client, stop)

    # Act
    worker.start()
    _wait_until(lambda: len(_fact_ids(session_factory)) == 1, worker)
    total = worker.finish(stop)

    # Assert
    assert total == 1
    assert _fact_ids(session_factory) == [event.payment_id]
    assert _pending(redis_client) == 0


def test_run_consumer_keeps_reading_and_totals_events_that_arrive_after_it_started(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    first_wave = [_event(), _event(declined=False)]
    second_wave = [_event()]
    stop = threading.Event()
    worker = _run_consumer_in_worker(session_factory, redis_client, stop)
    worker.start()
    time.sleep(0.3)

    # Act
    for event in first_wave:
        _stream_add(redis_client, event)
    _wait_until(lambda: len(_fact_ids(session_factory)) == 2, worker)
    for event in second_wave:
        _stream_add(redis_client, event)
    _wait_until(lambda: len(_fact_ids(session_factory)) == 3, worker)
    total = worker.finish(stop)

    # Assert
    expected = sorted(event.payment_id for event in [*first_wave, *second_wave])
    assert total == 3
    assert sorted(_fact_ids(session_factory)) == expected
    assert _pending(redis_client) == 0


def test_run_consumer_with_the_stop_event_already_set_returns_zero_and_projects_nothing(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    stop = threading.Event()
    worker = _run_consumer_in_worker(session_factory, redis_client, stop)

    # Act
    stop.set()
    worker.start()
    total = worker.finish(stop)

    # Assert
    assert total == 0
    assert _fact_ids(session_factory) == []


def test_run_consumer_survives_a_redis_error_logs_only_the_class_name_and_projects_on_retry(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)
    event = _event()
    _stream_add(redis_client, event)
    flaky = _Flaky(original=redis_client.xreadgroup, error=RedisConnectionError(REDIS_CANARY), failures=2)
    monkeypatch.setattr(redis_client, "xreadgroup", flaky)
    stop = threading.Event()
    worker = _run_consumer_in_worker(session_factory, redis_client, stop)

    # Act
    worker.start()
    _wait_until(lambda: len(_fact_ids(session_factory)) == 1, worker)
    total = worker.finish(stop)

    # Assert
    captured = capsys.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert total == 1
    assert flaky.calls >= 3
    assert _fact_ids(session_factory) == [event.payment_id]
    assert "ConnectionError" in logged
    assert REDIS_CANARY not in logged


def test_run_consumer_survives_a_database_error_logs_only_the_class_name_and_still_projects_the_event(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)
    event = _event()
    _stream_add(redis_client, event)
    error = OperationalError("SELECT 1", {}, Exception(DB_CANARY))
    flaky = _Flaky(original=session_factory.begin, error=error, failures=1)
    monkeypatch.setattr(session_factory, "begin", flaky)
    stop = threading.Event()
    worker = _run_consumer_in_worker(session_factory, redis_client, stop)

    # Act
    worker.start()
    _wait_until(lambda: len(_fact_ids(session_factory)) == 1, worker)
    worker.finish(stop)

    # Assert
    captured = capsys.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert _fact_ids(session_factory) == [event.payment_id]
    assert _pending(redis_client) == 0
    assert "OperationalError" in logged
    assert DB_CANARY not in logged


def test_run_consumer_sleeps_between_failing_iterations_instead_of_spinning(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    always_failing = _Flaky(
        original=redis_client.xreadgroup, error=RedisConnectionError(REDIS_CANARY), failures=10**9
    )
    monkeypatch.setattr(redis_client, "xreadgroup", always_failing)
    stop = threading.Event()
    worker = _run_consumer_in_worker(session_factory, redis_client, stop)

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
def test_main_projects_events_as_ops_ai_hostname_and_exits_zero_on_the_stop_signal(
    session_factory: sessionmaker[Session], redis_client: Redis, stop_signal: signal.Signals
) -> None:
    # Arrange
    event = _event()
    _stream_add(redis_client, event)
    process = subprocess.Popen(
        [sys.executable, "-m", "ops_ai.ingest.worker"],
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Act
    try:
        deadline = time.monotonic() + WAIT_SECONDS + 10
        while time.monotonic() < deadline and process.poll() is None:
            if _fact_ids(session_factory):
                break
            time.sleep(0.1)
        facts = _fact_ids(session_factory)
        consumers = _consumer_names(redis_client)
        process.send_signal(stop_signal)
        _, stderr = process.communicate(timeout=WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    # Assert
    assert facts == [event.payment_id]
    assert consumers == [f"ops-ai-{socket.gethostname()}"]
    assert process.returncode == 0
    assert "Traceback" not in stderr
