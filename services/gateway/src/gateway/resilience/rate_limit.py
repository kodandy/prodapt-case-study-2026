import hashlib
import json
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import anyio.to_thread
from redis import Redis
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Receive, Scope, Send

from gateway.accounts.sessions import InvalidSessionError, verify_session_token
from gateway.clock import Clock
from gateway.observability.metrics import RATE_LIMIT_FAIL_OPEN, RATE_LIMITED

RateScope = Literal["merchant", "user"]
MERCHANT_PREFIXES = ("/v1/payments", "/v1/disputes", "/v1/merchant-tokens")
USER_PREFIX = "/v1/me/"
REJECTED_BODY = json.dumps({"detail": "rate limit exceeded"}).encode()

# KEYS[1] bucket hash; ARGV now, rate per second, burst. Returns {allowed (1|0), retry_after_seconds}.
# Runs as one Redis script, so concurrent callers on the same key are serialised.
_BUCKET_SCRIPT = """
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
  tokens = burst
  ts = now
end
if ts > now then
  ts = now
end
if now > ts then
  tokens = math.min(burst, tokens + (now - ts) * rate)
  ts = now
end
local allowed = 0
local retry_after = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry_after = math.max(1, math.ceil((1 - tokens) / rate))
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(ts))
redis.call('EXPIRE', KEYS[1], math.ceil(burst / rate) + 1)
return {allowed, retry_after}
"""


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    retry_after_seconds: int


_ALLOWED = RateDecision(allowed=True, retry_after_seconds=0)


class TokenBucketLimiter:
    """Redis token bucket per (scope, key). Fails open: without Redis, or on any Redis error, the request is
    allowed (and the error counted), because rate limiting must never take payments down with Redis."""

    def __init__(self, redis: Redis | None, now: Callable[[], float] = time.time) -> None:
        self._redis = redis
        self._now = now
        self._script = None if redis is None else redis.register_script(_BUCKET_SCRIPT)

    def check(self, scope: RateScope, key: str, rate_per_second: float, burst: int) -> RateDecision:
        if self._script is None:
            return _ALLOWED
        try:
            raw = self._script(
                keys=[f"rl:{scope}:{key}"], args=[repr(self._now()), repr(rate_per_second), burst]
            )
        except RedisError:
            RATE_LIMIT_FAIL_OPEN.labels(scope).inc()
            return _ALLOWED
        allowed, retry_after = (int(value) for value in raw)
        if allowed:
            return _ALLOWED
        RATE_LIMITED.labels(scope).inc()
        return RateDecision(allowed=False, retry_after_seconds=max(retry_after, 1))


@dataclass(frozen=True)
class RateLimitConfig:
    merchant_rps: float = 200.0
    merchant_burst: int = 400
    user_rps: float = 5.0
    user_burst: int = 20
    enabled: bool = True


def _validated(config: RateLimitConfig) -> RateLimitConfig:
    for name, rate, burst in (
        ("merchant", config.merchant_rps, config.merchant_burst),
        ("user", config.user_rps, config.user_burst),
    ):
        if not math.isfinite(rate) or rate <= 0 or burst < 1:
            raise ValueError(f"{name} rate limit needs a finite rps > 0 and a burst >= 1")
    return config


def load_rate_limit_config() -> RateLimitConfig:
    defaults = RateLimitConfig()
    enabled = os.environ.get("GATEWAY_RATE_LIMIT_ENABLED")
    return _validated(
        RateLimitConfig(
            merchant_rps=float(os.environ.get("GATEWAY_RATE_LIMIT_MERCHANT_RPS", defaults.merchant_rps)),
            merchant_burst=int(os.environ.get("GATEWAY_RATE_LIMIT_MERCHANT_BURST", defaults.merchant_burst)),
            user_rps=float(os.environ.get("GATEWAY_RATE_LIMIT_USER_RPS", defaults.user_rps)),
            user_burst=int(os.environ.get("GATEWAY_RATE_LIMIT_USER_BURST", defaults.user_burst)),
            enabled=defaults.enabled
            if enabled is None
            else enabled.strip().lower() not in ("false", "0", "no", "off"),
        )
    )


def _bearer(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == b"authorization":
            scheme, _, token = value.decode("latin-1").partition(" ")
            return (token.strip() or None) if scheme.lower() == "bearer" else None
    return None


class RateLimitMiddleware:
    """Pure ASGI. Merchant bucket: /v1/payments, /v1/disputes, /v1/merchant-tokens with a bearer, keyed by the
    sha256 of the API key (the raw key never reaches Redis; every distinct bearer gets its own bucket, so this
    does not bound key guessing, which is left to the edge proxy). User bucket: /v1/me/* with a valid gateway
    session, keyed by user id. Anything else, and a missing or invalid session, passes through so the route
    answers as before. Session verification runs in a worker thread, off the event loop."""

    def __init__(
        self,
        app: ASGIApp,
        limiter: TokenBucketLimiter,
        config: RateLimitConfig,
        jwt_private_key_pem: str | None,
        clock: Clock,
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._config = config
        self._jwt_private_key_pem = jwt_private_key_pem
        self._clock = clock

    def _bucket(self, scope: Scope) -> tuple[RateScope, str, float, int] | None:
        path: str = scope["path"]
        token = _bearer(scope)
        if token is None:
            return None
        if path.startswith(MERCHANT_PREFIXES):
            digest = hashlib.sha256(token.encode()).hexdigest()
            return "merchant", digest, self._config.merchant_rps, self._config.merchant_burst
        if path.startswith(USER_PREFIX) and self._jwt_private_key_pem is not None:
            if token.count(".") != 2:
                return None
            try:
                claims = verify_session_token(self._jwt_private_key_pem, token, self._clock())
            except InvalidSessionError:
                return None
            return "user", str(claims.user_id), self._config.user_rps, self._config.user_burst
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._config.enabled:
            await self.app(scope, receive, send)
            return
        bucket = await anyio.to_thread.run_sync(self._bucket, scope)
        if bucket is None:
            await self.app(scope, receive, send)
            return
        decision = await anyio.to_thread.run_sync(self._limiter.check, *bucket)
        if decision.allowed:
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(REJECTED_BODY)).encode()),
                    (b"retry-after", str(decision.retry_after_seconds).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": REJECTED_BODY})
