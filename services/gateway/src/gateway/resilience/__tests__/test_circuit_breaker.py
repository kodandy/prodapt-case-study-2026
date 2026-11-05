"""Tests for gateway.resilience.circuit_breaker.CircuitBreaker (no I/O; time is injected)."""

import threading
import uuid
from typing import Any

from gateway.observability.metrics import REGISTRY
from gateway.resilience.circuit_breaker import CircuitBreaker

COOL_DOWN = 30.0


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _breaker(clock: _Clock, route: str | None = None, **overrides: Any) -> CircuitBreaker:
    return CircuitBreaker(
        route or f"route-{uuid.uuid4().hex}", monotonic=clock, cool_down_seconds=COOL_DOWN, **overrides
    )


def _gauge(route: str) -> float | None:
    return REGISTRY.get_sample_value("gateway_circuit_state", {"route": route})


def _fail(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        breaker.record_failure()


def _succeed(breaker: CircuitBreaker, times: int) -> None:
    for _ in range(times):
        breaker.record_success()


def _open(clock: _Clock) -> CircuitBreaker:
    breaker = _breaker(clock)
    _fail(breaker, 10)
    return breaker


def test_new_breaker_is_closed_and_allows_requests() -> None:
    # Arrange
    breaker = _breaker(_Clock())

    # Act
    allowed = breaker.allow_request()

    # Assert
    assert breaker.state == "closed"
    assert allowed is True


def test_breaker_stays_closed_below_min_calls_even_when_every_call_failed() -> None:
    # Arrange
    breaker = _breaker(_Clock())

    # Act
    _fail(breaker, 9)

    # Assert
    assert breaker.state == "closed"
    assert breaker.allow_request() is True


def test_breaker_opens_when_min_calls_is_reached_with_all_failures() -> None:
    # Arrange
    breaker = _breaker(_Clock())

    # Act
    _fail(breaker, 10)

    # Assert
    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_breaker_opens_when_the_failure_rate_equals_the_threshold() -> None:
    # Arrange
    breaker = _breaker(_Clock())
    _succeed(breaker, 5)

    # Act
    _fail(breaker, 5)

    # Assert
    assert breaker.state == "open"


def test_breaker_stays_closed_when_the_failure_rate_is_below_the_threshold() -> None:
    # Arrange
    breaker = _breaker(_Clock())
    _succeed(breaker, 6)

    # Act
    _fail(breaker, 4)

    # Assert
    assert breaker.state == "closed"
    assert breaker.allow_request() is True


def test_window_keeps_only_the_last_window_size_outcomes() -> None:
    # Arrange
    breaker = _breaker(_Clock(), failure_rate_threshold=0.75, window_size=4, min_calls=4)
    _succeed(breaker, 4)

    # Act / Assert
    _fail(breaker, 2)
    state_after_two_failures = breaker.state
    _fail(breaker, 1)
    state_after_three_failures = breaker.state
    assert state_after_two_failures == "closed"
    assert state_after_three_failures == "open"


def test_open_breaker_rejects_until_the_cool_down_has_elapsed() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)

    # Act
    clock.advance(COOL_DOWN - 1.0)

    # Assert
    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_state_reports_half_open_once_the_cool_down_elapsed_without_consuming_the_probe() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 1.0)

    # Act
    states = [breaker.state, breaker.state]
    probe_allowed = breaker.allow_request()

    # Assert
    assert states == ["half_open", "half_open"]
    assert probe_allowed is True


def test_half_open_admits_exactly_one_probe_while_it_is_in_flight() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 1.0)

    # Act
    decisions = [breaker.allow_request(), breaker.allow_request(), breaker.allow_request()]

    # Assert
    assert decisions == [True, False, False]
    assert breaker.state == "half_open"


def test_probe_success_closes_the_breaker_with_an_empty_window() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 1.0)
    breaker.allow_request()

    # Act
    breaker.record_success()

    # Assert
    assert breaker.state == "closed"
    assert breaker.allow_request() is True
    _fail(breaker, 9)
    assert breaker.state == "closed"


def test_probe_failure_reopens_the_breaker_and_restarts_the_cool_down() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 10.0)
    breaker.allow_request()

    # Act
    breaker.record_failure()
    clock.advance(COOL_DOWN - 1.0)
    still_open = (breaker.state, breaker.allow_request())
    clock.advance(2.0)

    # Assert
    assert still_open == ("open", False)
    assert breaker.state == "half_open"
    assert breaker.allow_request() is True


def test_allow_request_after_a_failed_probe_is_rejected_immediately() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 1.0)
    breaker.allow_request()
    breaker.record_failure()

    # Act
    allowed = breaker.allow_request()

    # Assert
    assert allowed is False
    assert breaker.state == "open"


def test_a_second_probe_is_admitted_after_the_next_cool_down_when_the_first_probe_failed() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 1.0)
    breaker.allow_request()
    breaker.record_failure()
    clock.advance(COOL_DOWN + 1.0)

    # Act
    decisions = [breaker.allow_request(), breaker.allow_request()]

    # Assert
    assert decisions == [True, False]


def test_circuit_gauge_follows_the_state_transitions() -> None:
    # Arrange
    clock = _Clock()
    route = f"route-{uuid.uuid4().hex}"
    breaker = _breaker(clock, route)

    # Act / Assert
    _fail(breaker, 10)
    assert _gauge(route) == 1.0
    clock.advance(COOL_DOWN + 1.0)
    breaker.allow_request()
    assert _gauge(route) == 2.0
    breaker.record_success()
    assert _gauge(route) == 0.0


def test_circuit_gauge_is_one_after_a_failed_probe() -> None:
    # Arrange
    clock = _Clock()
    route = f"route-{uuid.uuid4().hex}"
    breaker = _breaker(clock, route)
    _fail(breaker, 10)
    clock.advance(COOL_DOWN + 1.0)
    breaker.allow_request()

    # Act
    breaker.record_failure()

    # Assert
    assert _gauge(route) == 1.0


def test_concurrent_callers_get_exactly_one_probe_when_the_cool_down_has_elapsed() -> None:
    # Arrange
    clock = _Clock()
    breaker = _open(clock)
    clock.advance(COOL_DOWN + 1.0)
    callers = 16
    barrier = threading.Barrier(callers)
    decisions: list[bool] = []
    lock = threading.Lock()

    def call() -> None:
        barrier.wait(timeout=5)
        decision = breaker.allow_request()
        with lock:
            decisions.append(decision)

    threads = [threading.Thread(target=call) for _ in range(callers)]

    # Act
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    # Assert
    assert len(decisions) == callers
    assert decisions.count(True) == 1
    assert decisions.count(False) == callers - 1


def test_concurrent_failures_are_all_recorded_and_open_the_breaker() -> None:
    # Arrange
    breaker = _breaker(_Clock())
    barrier = threading.Barrier(10)

    def fail() -> None:
        barrier.wait(timeout=5)
        breaker.record_failure()

    threads = [threading.Thread(target=fail) for _ in range(10)]

    # Act
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    # Assert
    assert breaker.state == "open"
