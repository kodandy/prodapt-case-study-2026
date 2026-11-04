"""RED tests for gateway/notifications/worker.py, written from docs/specs/gateway-basic.md (B11 run_notifier).

`run_notifier` has the same loop contract as `run_relay`: it calls `process_pending_notifications` until
`stop` is set, sleeps `poll_interval_seconds` when a pass created nothing, logs a failed pass by class
name only, retries, and returns the total created.
"""

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

import gateway.notifications.worker as worker_module
import pytest
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.notifications.service import process_pending_notifications
from gateway.notifications.worker import run_notifier
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

WAIT_SECONDS = 15.0
POLL_INTERVAL_SECONDS = 0.05
ERROR_CANARY = "canary-error-detail-7f2c"


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
            pytest.fail(f"run_notifier did not return within {WAIT_SECONDS}s after stop was set")
        if self._error is not None:
            raise self._error
        if self._result is None:
            pytest.fail("run_notifier returned without a result")
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


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    merchant_id: uuid.UUID


def _harness(session_factory: sessionmaker[Session], clock: FixedClock) -> Harness:
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(deps, credentials.merchant_id)


def _charge(h: Harness, pan: str = "4242424242424242", amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _notification_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.notifications")).scalar_one())


def _notification_outbox_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM gateway.outbox WHERE topic = 'gw.notifications.v1'")
            ).scalar_one()
        )


def _notifier(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    stop: threading.Event,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> _Worker:
    return _Worker(
        lambda: run_notifier(session_factory, clock, stop, poll_interval_seconds=poll_interval_seconds)
    )


def test_run_notifier_records_the_pending_payment_and_returns_the_total_when_stopped(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop)

    # Act
    worker.start()
    _wait_until(lambda: _notification_count(session_factory) == 1, worker)
    total = worker.finish(stop)

    # Assert
    assert total == 1
    assert _notification_count(session_factory) == 1
    assert _notification_outbox_count(session_factory) == 1


def test_run_notifier_keeps_polling_and_totals_payments_that_arrive_after_it_started(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop)
    worker.start()
    time.sleep(0.3)

    # Act
    _charge(h)
    _charge(h, amount_minor=2500)
    _wait_until(lambda: _notification_count(session_factory) == 2, worker)
    _charge(h, amount_minor=3500)
    _wait_until(lambda: _notification_count(session_factory) == 3, worker)
    total = worker.finish(stop)

    # Assert
    assert total == 3
    assert _notification_count(session_factory) == 3
    assert _notification_outbox_count(session_factory) == 3


def test_run_notifier_counts_records_created_not_events_taken(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h, pan="4000000000000002")
    _charge(h)
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop)

    # Act
    worker.start()
    _wait_until(lambda: _notification_count(session_factory) == 1, worker)
    time.sleep(0.2)
    total = worker.finish(stop)

    # Assert
    assert total == 1
    assert _notification_count(session_factory) == 1


def test_run_notifier_with_the_stop_event_already_set_returns_zero_and_records_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop)

    # Act
    stop.set()
    worker.start()
    total = worker.finish(stop)

    # Assert
    assert total == 0
    assert _notification_count(session_factory) == 0


def test_run_notifier_stops_promptly_even_with_a_long_poll_interval(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop, poll_interval_seconds=60.0)
    worker.start()
    time.sleep(0.3)

    # Act
    started = time.monotonic()
    total = worker.finish(stop)
    elapsed = time.monotonic() - started

    # Assert
    assert total == 0
    assert elapsed < 5.0


@pytest.mark.parametrize(
    ("error", "class_name"),
    [
        (OperationalError("SELECT 1", {}, Exception(ERROR_CANARY)), "OperationalError"),
        (OSError(ERROR_CANARY), "OSError"),
    ],
    ids=["database_error", "os_error"],
)
def test_run_notifier_survives_a_failed_pass_logs_only_the_class_name_and_records_on_retry(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    class_name: str,
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG, logger="gateway")
    h = _harness(session_factory, clock)
    _charge(h)
    flaky = _Flaky(original=process_pending_notifications, error=error, failures=2)
    monkeypatch.setattr(worker_module, "process_pending_notifications", flaky)
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop)

    # Act
    worker.start()
    _wait_until(lambda: _notification_count(session_factory) == 1, worker)
    total = worker.finish(stop)

    # Assert
    captured = capsys.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert total == 1
    assert flaky.calls >= 3
    assert _notification_count(session_factory) == 1
    assert class_name in logged
    assert ERROR_CANARY not in logged


def test_run_notifier_sleeps_between_failing_passes_instead_of_spinning(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    always_failing = _Flaky(
        original=process_pending_notifications,
        error=OperationalError("SELECT 1", {}, Exception(ERROR_CANARY)),
        failures=10**9,
    )
    monkeypatch.setattr(worker_module, "process_pending_notifications", always_failing)
    stop = threading.Event()
    worker = _notifier(session_factory, clock, stop, poll_interval_seconds=0.1)

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
def test_main_records_pending_payments_and_exits_zero_on_the_stop_signal(
    session_factory: sessionmaker[Session], clock: FixedClock, stop_signal: signal.Signals
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)
    env = {
        **os.environ,
        "GATEWAY_API_KEY_PEPPER": "p" * 40,
        "GATEWAY_CARD_FINGERPRINT_KEY": "f" * 40,
    }
    env.pop("GATEWAY_SECRETS_DIR", None)
    process = subprocess.Popen(
        [sys.executable, "-m", "gateway.notifications.worker"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    # Act
    try:
        deadline = time.monotonic() + WAIT_SECONDS + 10
        while time.monotonic() < deadline and process.poll() is None:
            if _notification_count(session_factory) == 1:
                break
            time.sleep(0.1)
        recorded = _notification_count(session_factory)
        process.send_signal(stop_signal)
        _, stderr = process.communicate(timeout=WAIT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    # Assert
    assert recorded == 1
    assert _notification_outbox_count(session_factory) == 1
    assert process.returncode == 0
    assert "Traceback" not in stderr
