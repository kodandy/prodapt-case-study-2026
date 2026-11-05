"""Tests for gateway.resilience.rate_limit: the Redis token bucket, RateLimitConfig and its env loader."""

import dataclasses
import threading
from typing import Literal

import pytest
from gateway.observability.metrics import REGISTRY
from gateway.resilience.rate_limit import (
    RateDecision,
    RateLimitConfig,
    TokenBucketLimiter,
    load_rate_limit_config,
)
from redis import Redis

Scope = Literal["merchant", "user"]

ENV_NAMES = (
    "GATEWAY_RATE_LIMIT_MERCHANT_RPS",
    "GATEWAY_RATE_LIMIT_MERCHANT_BURST",
    "GATEWAY_RATE_LIMIT_USER_RPS",
    "GATEWAY_RATE_LIMIT_USER_BURST",
    "GATEWAY_RATE_LIMIT_ENABLED",
)


class _Now:
    """A controllable wall clock for the limiter."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value


def _counter(name: str, scope: str) -> float:
    value = REGISTRY.get_sample_value(name, {"scope": scope})
    return 0.0 if value is None else value


def _rejected(scope: str) -> float:
    return _counter("gateway_rate_limit_rejected_total", scope)


def _fail_open(scope: str) -> float:
    return _counter("gateway_rate_limit_fail_open_total", scope)


def _drain(
    limiter: TokenBucketLimiter, scope: Scope, key: str, rate: float, burst: int
) -> list[RateDecision]:
    return [limiter.check(scope, key, rate, burst) for _ in range(burst)]


def test_fresh_key_allows_exactly_burst_requests_then_denies_the_next(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())

    # Act
    decisions = [limiter.check("merchant", "k1", 1.0, 3) for _ in range(4)]

    # Assert
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert [d.retry_after_seconds for d in decisions[:3]] == [0, 0, 0]
    assert decisions[3].retry_after_seconds >= 1


def test_denied_decision_reports_a_retry_after_covering_the_refill_time(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())
    _drain(limiter, "merchant", "k1", 0.5, 1)

    # Act
    decision = limiter.check("merchant", "k1", 0.5, 1)

    # Assert
    assert decision.allowed is False
    assert decision.retry_after_seconds >= 2


def test_advancing_time_by_one_over_rate_allows_exactly_one_more_request(redis_client: Redis) -> None:
    # Arrange
    now = _Now()
    limiter = TokenBucketLimiter(redis_client, now=now)
    _drain(limiter, "merchant", "k1", 2.0, 3)
    assert limiter.check("merchant", "k1", 2.0, 3).allowed is False

    # Act
    now.value += 0.5
    after_refill = [limiter.check("merchant", "k1", 2.0, 3) for _ in range(2)]

    # Assert
    assert [d.allowed for d in after_refill] == [True, False]


def test_refill_is_capped_at_burst(redis_client: Redis) -> None:
    # Arrange
    now = _Now()
    limiter = TokenBucketLimiter(redis_client, now=now)
    _drain(limiter, "merchant", "k1", 1.0, 3)
    now.value += 10_000.0

    # Act
    decisions = [limiter.check("merchant", "k1", 1.0, 3) for _ in range(4)]

    # Assert
    assert [d.allowed for d in decisions] == [True, True, True, False]


def test_buckets_are_independent_per_key(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())
    _drain(limiter, "merchant", "k1", 1.0, 1)

    # Act
    exhausted = limiter.check("merchant", "k1", 1.0, 1)
    other_key = limiter.check("merchant", "k2", 1.0, 1)

    # Assert
    assert exhausted.allowed is False
    assert other_key == RateDecision(allowed=True, retry_after_seconds=0)


def test_buckets_are_independent_per_scope_for_the_same_key(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())
    _drain(limiter, "merchant", "same", 1.0, 1)

    # Act
    merchant = limiter.check("merchant", "same", 1.0, 1)
    user = limiter.check("user", "same", 1.0, 1)

    # Assert
    assert merchant.allowed is False
    assert user.allowed is True


def test_bucket_lives_under_the_scope_prefixed_redis_key(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())

    # Act
    limiter.check("user", "abc", 1.0, 3)

    # Assert
    assert redis_client.exists("rl:user:abc") == 1
    assert redis_client.keys("rl:*") == ["rl:user:abc"]


def test_rejection_increments_the_rejected_counter_for_its_scope_only(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())
    _drain(limiter, "user", "u1", 1.0, 2)
    before_user = _rejected("user")
    before_merchant = _rejected("merchant")

    # Act
    limiter.check("user", "u1", 1.0, 2)
    limiter.check("user", "u1", 1.0, 2)

    # Assert
    assert _rejected("user") - before_user == 2.0
    assert _rejected("merchant") - before_merchant == 0.0


def test_allowed_requests_do_not_increment_the_rejected_counter(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())
    before = _rejected("merchant")

    # Act
    _drain(limiter, "merchant", "k1", 1.0, 3)

    # Assert
    assert _rejected("merchant") - before == 0.0


def test_concurrent_checks_never_admit_more_than_burst(redis_client: Redis) -> None:
    # Arrange
    limiter = TokenBucketLimiter(redis_client, now=_Now())
    callers = 20
    burst = 5
    barrier = threading.Barrier(callers)
    results: list[bool] = []
    lock = threading.Lock()

    def call() -> None:
        barrier.wait(timeout=5)
        decision = limiter.check("merchant", "shared", 0.001, burst)
        with lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=call) for _ in range(callers)]

    # Act
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    # Assert
    assert len(results) == callers
    assert results.count(True) == burst
    assert results.count(False) == callers - burst


def test_limiter_without_redis_always_allows() -> None:
    # Arrange
    limiter = TokenBucketLimiter(None)

    # Act
    decisions = [limiter.check("merchant", "k1", 0.001, 1) for _ in range(50)]

    # Assert
    assert decisions == [RateDecision(allowed=True, retry_after_seconds=0)] * 50


def test_redis_error_fails_open_and_increments_the_fail_open_counter() -> None:
    # Arrange
    dead_redis = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    limiter = TokenBucketLimiter(dead_redis, now=_Now())
    before_fail_open = _fail_open("user")
    before_rejected = _rejected("user")

    # Act
    decision = limiter.check("user", "u1", 0.001, 1)

    # Assert
    assert decision == RateDecision(allowed=True, retry_after_seconds=0)
    assert _fail_open("user") - before_fail_open == 1.0
    assert _rejected("user") - before_rejected == 0.0


def test_rate_decision_is_frozen() -> None:
    # Arrange
    decision = RateDecision(allowed=True, retry_after_seconds=0)

    # Act / Assert
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.allowed = False  # type: ignore[misc]


def test_rate_limit_config_defaults() -> None:
    # Arrange / Act
    config = RateLimitConfig()

    # Assert
    assert (config.merchant_rps, config.merchant_burst) == (200.0, 400)
    assert (config.user_rps, config.user_burst) == (5.0, 20)
    assert config.enabled is True


def test_rate_limit_config_is_frozen() -> None:
    # Arrange
    config = RateLimitConfig()

    # Act / Assert
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.enabled = False  # type: ignore[misc]


def test_load_rate_limit_config_without_env_returns_the_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)

    # Act
    config = load_rate_limit_config()

    # Assert
    assert config == RateLimitConfig()


def test_load_rate_limit_config_reads_every_env_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_MERCHANT_RPS", "12.5")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_MERCHANT_BURST", "30")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_USER_RPS", "0.5")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_USER_BURST", "3")
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_ENABLED", "false")

    # Act
    config = load_rate_limit_config()

    # Assert
    assert config == RateLimitConfig(
        merchant_rps=12.5, merchant_burst=30, user_rps=0.5, user_burst=3, enabled=False
    )


def test_load_rate_limit_config_keeps_defaults_for_unset_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_RATE_LIMIT_USER_BURST", "7")

    # Act
    config = load_rate_limit_config()

    # Assert
    assert config == RateLimitConfig(user_burst=7)
