import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Literal

from gateway.observability.metrics import CIRCUIT_STATE, PROVIDER_CALLS
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult, ProviderTimeoutError

BreakerState = Literal["closed", "open", "half_open"]
_GAUGE_VALUE: dict[BreakerState, int] = {"closed": 0, "open": 1, "half_open": 2}
PROVIDER_UNAVAILABLE = "provider_unavailable"
PROVIDER_WORKERS = 32


class CircuitBreaker:
    """Per-route breaker on a sliding window of the last `window_size` outcomes.

    Opens once the window holds >= `min_calls` outcomes and the failure share reaches the threshold; after
    `cool_down_seconds` it admits exactly one probe (half_open): success closes it, failure re-opens it.
    """

    def __init__(
        self,
        route: str,
        *,
        failure_rate_threshold: float = 0.5,
        window_size: int = 20,
        min_calls: int = 10,
        cool_down_seconds: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._route = route
        self._threshold = failure_rate_threshold
        self._min_calls = min_calls
        self._cool_down = cool_down_seconds
        self._monotonic = monotonic
        self._window: deque[bool] = deque(maxlen=window_size)
        self._opened_at: float | None = None
        self._probe_in_flight = False
        self._lock = threading.Lock()

    @property
    def route(self) -> str:
        return self._route

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._state()

    def _state(self) -> BreakerState:
        if self._opened_at is None:
            return "closed"
        if self._probe_in_flight or self._monotonic() - self._opened_at >= self._cool_down:
            return "half_open"
        return "open"

    def _publish(self) -> None:
        CIRCUIT_STATE.labels(self._route).set(_GAUGE_VALUE[self._state()])

    def allow_request(self) -> bool:
        with self._lock:
            state = self._state()
            if state == "closed":
                return True
            if state == "open" or self._probe_in_flight:
                return False
            self._probe_in_flight = True
            self._publish()
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                self._opened_at = None
                self._probe_in_flight = False
                self._window.clear()
                self._publish()
                return
            self._window.append(True)

    def record_failure(self) -> None:
        with self._lock:
            if self._opened_at is not None:
                self._opened_at = self._monotonic()
                self._probe_in_flight = False
                self._publish()
                return
            self._window.append(False)
            failures = self._window.count(False)
            if len(self._window) >= self._min_calls and failures / len(self._window) >= self._threshold:
                self._opened_at = self._monotonic()
                self._publish()


class ResilientProvider:
    """ProviderPort wrapper: a call deadline and a per-route circuit breaker around one acquirer.

    A timed-out or failed call raises (charge_card records `unknown`) and is never retried or rerouted here,
    because the acquirer may have charged. An open breaker, or a deadline hit while the call was still queued
    (cancelled before it started), declines with `provider_unavailable` without contacting the acquirer, so
    that attempt is known not to have been charged; risk scoring ignores those declines.
    """

    def __init__(self, inner: ProviderPort, breaker: CircuitBreaker, timeout_seconds: float) -> None:
        self.name = inner.name
        self._inner = inner
        self._breaker = breaker
        self._timeout = timeout_seconds
        self._pool = ThreadPoolExecutor(
            max_workers=PROVIDER_WORKERS, thread_name_prefix=f"provider-{inner.name}"
        )

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        if not self._breaker.allow_request():
            PROVIDER_CALLS.labels(self._breaker.route, "short_circuited").inc()
            return self._unavailable()
        future = self._pool.submit(self._inner.charge, provider_key, amount_minor, currency, card)
        done, _ = wait([future], timeout=self._timeout)
        if not done:
            if future.cancel():
                PROVIDER_CALLS.labels(self._breaker.route, "short_circuited").inc()
                return self._unavailable()
            self._failed("timeout")
            raise ProviderTimeoutError(f"{self.name} did not answer within {self._timeout}s")
        try:
            result = future.result()
        except ProviderTimeoutError:
            self._failed("timeout")
            raise
        except Exception:
            self._failed("error")
            raise
        self._breaker.record_success()
        PROVIDER_CALLS.labels(self._breaker.route, result.status).inc()
        return result

    def _unavailable(self) -> ProviderResult:
        return ProviderResult(
            status="declined",
            provider_ref=f"breaker_{uuid.uuid4().hex[:20]}",
            decline_code=PROVIDER_UNAVAILABLE,
        )

    def _failed(self, outcome: str) -> None:
        self._breaker.record_failure()
        PROVIDER_CALLS.labels(self._breaker.route, outcome).inc()

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)
