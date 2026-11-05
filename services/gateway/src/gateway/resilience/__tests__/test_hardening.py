"""Hardening tests: queued-call cancellation in ResilientProvider, token-bucket clock skew, rate-limit config
validation, and the /v1/me/ middleware skipping JWT verification for bearers that cannot be a JWT."""

import contextlib
import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from gateway.clock import FixedClock
from gateway.payments.providers.port import CardRef, ProviderResult, ProviderTimeoutError
from gateway.resilience import circuit_breaker
from gateway.resilience import rate_limit as rate_limit_module
from gateway.resilience.circuit_breaker import PROVIDER_UNAVAILABLE, CircuitBreaker, ResilientProvider
from gateway.resilience.rate_limit import (
    RateDecision,
    RateLimitConfig,
    RateLimitMiddleware,
    TokenBucketLimiter,
    load_rate_limit_config,
)
from redis import Redis

CARD = CardRef(token="tok_test", bin="424242", last4="4242")
SUCCEEDED = ProviderResult(status="succeeded", provider_ref="acq_a_ok", decline_code=None)
BLOCKER_KEY = "blocker"
QUEUED_KEY = "queued"
QUEUE_DEADLINE_SECONDS = 0.1


class _GatedInner:
    """Records every provider key that actually starts; the BLOCKER_KEY call waits on `release`."""

    name = "acq_a"

    def __init__(self, release: threading.Event) -> None:
        self._release = release
        self._lock = threading.Lock()
        self.started_keys: list[str] = []
        self.blocker_started = threading.Event()

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self.started_keys.append(provider_key)
        if provider_key == BLOCKER_KEY:
            self.blocker_started.set()
            self._release.wait(5.0)
        return SUCCEEDED

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return None


class _Now:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def release() -> Iterator[threading.Event]:
    """Frees any inner call still blocked when the test ends."""
    event = threading.Event()
    yield event
    event.set()


def _single_worker_provider(
    monkeypatch: pytest.MonkeyPatch, inner: _GatedInner, timeout_seconds: float, min_calls: int = 1
) -> tuple[ResilientProvider, CircuitBreaker]:
    monkeypatch.setattr(circuit_breaker, "PROVIDER_WORKERS", 1)
    breaker = CircuitBreaker(f"route-{uuid.uuid4().hex}", min_calls=min_calls)
    return ResilientProvider(inner, breaker, timeout_seconds=timeout_seconds), breaker


def _occupy_the_only_worker(provider: ResilientProvider, inner: _GatedInner) -> threading.Thread:
    """Starts a caller whose inner call holds the only worker; that caller itself times out, as designed."""

    def blocker_call() -> None:
        with contextlib.suppress(ProviderTimeoutError):
            provider.charge(BLOCKER_KEY, 1500, "USD", CARD)

    thread = threading.Thread(target=blocker_call, name="blocker-caller")
    thread.start()
    assert inner.blocker_started.wait(2.0) is True
    return thread


# --- ResilientProvider: deadline hit while the call is still queued --------------------------------------


def test_deadline_expiring_while_the_call_is_queued_declines_provider_unavailable_and_never_runs_it(
    monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> None:
    # Arrange
    inner = _GatedInner(release)
    provider, _ = _single_worker_provider(monkeypatch, inner, timeout_seconds=QUEUE_DEADLINE_SECONDS)
    blocker = _occupy_the_only_worker(provider, inner)

    # Act
    result = provider.charge(QUEUED_KEY, 1500, "USD", CARD)
    release.set()
    blocker.join(5.0)
    time.sleep(0.1)  # the freed worker would run the queued call now if it had not been cancelled

    # Assert
    assert result.status == "declined"
    assert result.decline_code == PROVIDER_UNAVAILABLE
    assert result.provider_ref.startswith("breaker_")
    assert inner.started_keys == [BLOCKER_KEY]


def test_queued_call_cancelled_by_the_deadline_does_not_count_as_a_breaker_failure(
    monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> None:
    # Arrange
    inner = _GatedInner(release)
    # min_calls=2: the blocker caller's own timeout is one failure; a second one would open the breaker
    provider, breaker = _single_worker_provider(
        monkeypatch, inner, timeout_seconds=QUEUE_DEADLINE_SECONDS, min_calls=2
    )
    blocker = _occupy_the_only_worker(provider, inner)

    # Act
    provider.charge(QUEUED_KEY, 1500, "USD", CARD)
    blocker.join(5.0)

    # Assert
    assert breaker.state == "closed"


def test_inner_call_already_started_past_the_deadline_still_raises_provider_timeout(
    monkeypatch: pytest.MonkeyPatch, release: threading.Event
) -> None:
    # Arrange
    inner = _GatedInner(release)
    provider, breaker = _single_worker_provider(monkeypatch, inner, timeout_seconds=QUEUE_DEADLINE_SECONDS)

    # Act
    with pytest.raises(ProviderTimeoutError) as raised:
        provider.charge(BLOCKER_KEY, 1500, "USD", CARD)

    # Assert
    assert str(raised.value) == f"acq_a did not answer within {QUEUE_DEADLINE_SECONDS}s"
    assert inner.started_keys == [BLOCKER_KEY]
    assert breaker.state == "open"


# --- TokenBucketLimiter: stored ts in the future (clock skew between replicas) ---------------------------


def test_stored_timestamp_in_the_future_does_not_lock_the_bucket_out(redis_client: Redis) -> None:
    # Arrange
    now = _Now(1000.0)
    limiter = TokenBucketLimiter(redis_client, now=now)

    # Act
    first = limiter.check("merchant", "skew", 1.0, 1)
    now.value = 500.0
    skewed = limiter.check("merchant", "skew", 1.0, 1)
    now.value = 1001.0
    recovered = limiter.check("merchant", "skew", 1.0, 1)

    # Assert
    assert first == RateDecision(allowed=True, retry_after_seconds=0)
    assert skewed == RateDecision(allowed=False, retry_after_seconds=1)
    assert recovered == RateDecision(allowed=True, retry_after_seconds=0)


# --- load_rate_limit_config: invalid values are rejected -------------------------------------------------


@pytest.mark.parametrize("rps", ["0", "-1", "nan", "inf"])
def test_load_rate_limit_config_rejects_a_non_positive_or_non_finite_merchant_rps(
    monkeypatch: pytest.MonkeyPatch, rps: str
) -> None:
    # Arrange
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_MERCHANT_RPS", rps)

    # Act
    with pytest.raises(ValueError) as raised:
        load_rate_limit_config()

    # Assert
    assert str(raised.value) == "merchant rate limit needs a finite rps > 0 and a burst >= 1"


def test_load_rate_limit_config_rejects_a_zero_user_burst(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_USER_BURST", "0")

    # Act
    with pytest.raises(ValueError) as raised:
        load_rate_limit_config()

    # Assert
    assert str(raised.value) == "user rate limit needs a finite rps > 0 and a burst >= 1"


# --- RateLimitMiddleware: a bearer that cannot be a JWT never reaches verify_session_token ---------------


def test_me_route_with_a_bearer_without_two_dots_passes_through_without_verifying_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    verify_calls: list[str] = []

    def recording_verify(*args: object, **kwargs: object) -> None:
        verify_calls.append("called")
        raise AssertionError("verify_session_token must not be called for a non-JWT bearer")

    monkeypatch.setattr(rate_limit_module, "verify_session_token", recording_verify)
    app = FastAPI()

    @app.get("/v1/me/ping")
    def ping() -> PlainTextResponse:
        return PlainTextResponse("pong", status_code=202)

    app.add_middleware(
        RateLimitMiddleware,
        limiter=TokenBucketLimiter(None),
        config=RateLimitConfig(),
        jwt_private_key_pem="unused-key-pem",
        clock=FixedClock(datetime(2026, 11, 3, 12, 0, tzinfo=UTC)),
    )
    client = TestClient(app)

    # Act
    response = client.get("/v1/me/ping", headers={"Authorization": "Bearer notajwt"})

    # Assert
    assert response.status_code == 202
    assert response.text == "pong"
    assert verify_calls == []
