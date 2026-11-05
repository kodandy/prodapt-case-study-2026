"""Rate-limit middleware wired into create_app: merchant and user buckets, 429s, disabled and fail-open."""

import functools
import hashlib
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.observability.metrics import REGISTRY
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.resilience.rate_limit import RateLimitConfig
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

TIGHT_MERCHANT = RateLimitConfig(merchant_rps=0.001, merchant_burst=2)
TIGHT_USER = RateLimitConfig(user_rps=0.001, user_burst=2)


@functools.cache
def _jwt_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _counter(name: str, scope: str) -> float:
    value = REGISTRY.get_sample_value(name, {"scope": scope})
    return 0.0 if value is None else value


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    redis: Redis | None,
    rate_limits: RateLimitConfig,
) -> TestClient:
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=redis,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    app_deps = GatewayAppDeps(
        charge=deps,
        api_key_pepper=TEST_API_KEY_PEPPER,
        jwt_private_key_pem=_jwt_private_key_pem(),
        rate_limits=rate_limits,
    )
    return TestClient(create_app(app_deps))


def _api_key(session_factory: sessionmaker[Session], clock: FixedClock, name: str = "Acme") -> str:
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return credentials.api_key


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _session_token(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str
) -> tuple[uuid.UUID, str]:
    with session_factory() as session:
        user_id = sign_up(session, clock, email, "correct horse").user_id
        session.commit()
    return user_id, issue_session_token(_jwt_private_key_pem(), user_id, clock(), ("pwd",))


def test_merchant_bucket_allows_burst_requests_then_returns_429_with_retry_after(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))

    # Act
    responses = [client.get("/v1/payments", headers=headers) for _ in range(3)]

    # Assert
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert responses[2].json() == {"detail": "rate limit exceeded"}
    assert int(responses[2].headers["Retry-After"]) >= 1


def test_a_second_merchants_key_has_its_own_bucket(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    first = _bearer(_api_key(session_factory, clock, "First"))
    second = _bearer(_api_key(session_factory, clock, "Second"))
    for _ in range(3):
        client.get("/v1/payments", headers=first)

    # Act
    response = client.get("/v1/payments", headers=second)

    # Assert
    assert response.status_code == 200


def test_bucket_key_is_the_sha256_of_the_api_key_and_the_raw_key_is_never_stored(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    api_key = _api_key(session_factory, clock)

    # Act
    client.get("/v1/payments", headers=_bearer(api_key))

    # Assert
    digest = hashlib.sha256(api_key.encode()).hexdigest()
    assert redis_client.keys("rl:*") == [f"rl:merchant:{digest}"]
    assert all(api_key not in key for key in redis_client.keys("*"))


def test_merchant_bucket_is_shared_across_payments_disputes_and_merchant_tokens(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))

    # Act
    payments = client.get("/v1/payments", headers=headers)
    disputes = client.get(f"/v1/disputes/{uuid.uuid4()}", headers=headers)
    tokens = client.post("/v1/merchant-tokens", headers=headers)

    # Assert
    assert payments.status_code == 200
    assert disputes.status_code == 404
    assert tokens.status_code == 429


def test_rejection_increments_the_merchant_rejected_counter(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))
    before = _counter("gateway_rate_limit_rejected_total", "merchant")

    # Act
    for _ in range(4):
        client.get("/v1/payments", headers=headers)

    # Assert
    assert _counter("gateway_rate_limit_rejected_total", "merchant") - before == 2.0


def test_requests_without_a_bearer_are_never_rate_limited(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)

    # Act
    statuses = [client.get("/v1/payments").status_code for _ in range(5)]

    # Assert
    assert statuses == [401, 401, 401, 401, 401]
    assert redis_client.keys("rl:*") == []


def test_paths_outside_the_limited_prefixes_are_not_rate_limited(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))

    # Act
    statuses = [client.get("/healthz", headers=headers).status_code for _ in range(5)]

    # Assert
    assert statuses == [200, 200, 200, 200, 200]
    assert redis_client.keys("rl:*") == []


def test_user_bucket_allows_burst_requests_then_returns_429_with_retry_after(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_USER)
    user_id, token = _session_token(session_factory, clock, "alice@example.com")

    # Act
    responses = [client.get("/v1/me/wallets", headers=_bearer(token)) for _ in range(3)]

    # Assert
    assert [r.status_code for r in responses] == [200, 200, 429]
    assert responses[2].json() == {"detail": "rate limit exceeded"}
    assert int(responses[2].headers["Retry-After"]) >= 1
    assert redis_client.keys("rl:*") == [f"rl:user:{user_id}"]


def test_a_second_users_session_has_its_own_bucket(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_USER)
    _, first_token = _session_token(session_factory, clock, "alice@example.com")
    _, second_token = _session_token(session_factory, clock, "bob@example.com")
    for _ in range(3):
        client.get("/v1/me/wallets", headers=_bearer(first_token))

    # Act
    response = client.get("/v1/me/wallets", headers=_bearer(second_token))

    # Assert
    assert response.status_code == 200


def test_user_rejection_increments_the_user_rejected_counter(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_USER)
    _, token = _session_token(session_factory, clock, "alice@example.com")
    before = _counter("gateway_rate_limit_rejected_total", "user")

    # Act
    for _ in range(3):
        client.get("/v1/me/wallets", headers=_bearer(token))

    # Assert
    assert _counter("gateway_rate_limit_rejected_total", "user") - before == 1.0


def test_an_invalid_session_bearer_on_me_routes_is_not_limited_and_still_gets_401(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_USER)

    # Act
    headers = _bearer("not-a-session-token")
    statuses = [client.get("/v1/me/wallets", headers=headers).status_code for _ in range(4)]

    # Assert
    assert statuses == [401, 401, 401, 401]
    assert redis_client.keys("rl:*") == []


def test_a_merchant_api_key_on_me_routes_is_not_limited_by_the_user_bucket(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_USER)
    headers = _bearer(_api_key(session_factory, clock))

    # Act
    statuses = [client.get("/v1/me/wallets", headers=headers).status_code for _ in range(4)]

    # Assert
    assert statuses == [401, 401, 401, 401]


def test_disabled_rate_limiting_never_returns_429(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    disabled = RateLimitConfig(merchant_rps=0.001, merchant_burst=2, enabled=False)
    client = _client(session_factory, clock, redis_client, disabled)
    headers = _bearer(_api_key(session_factory, clock))

    # Act
    statuses = [client.get("/v1/payments", headers=headers).status_code for _ in range(5)]

    # Assert
    assert statuses == [200, 200, 200, 200, 200]
    assert redis_client.keys("rl:*") == []


def test_without_a_redis_client_nothing_is_limited(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, None, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))

    # Act
    statuses = [client.get("/v1/payments", headers=headers).status_code for _ in range(5)]

    # Assert
    assert statuses == [200, 200, 200, 200, 200]


def test_with_redis_down_requests_still_succeed_and_the_fail_open_counter_increments(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    dead_redis = Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    client = _client(session_factory, clock, dead_redis, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))
    before = _counter("gateway_rate_limit_fail_open_total", "merchant")

    # Act
    response = client.get("/v1/payments", headers=headers)

    # Assert
    assert response.status_code == 200
    assert _counter("gateway_rate_limit_fail_open_total", "merchant") - before == 1.0


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_operational_endpoints_stay_reachable_after_the_merchant_bucket_is_exhausted(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock, path: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, redis_client, TIGHT_MERCHANT)
    headers = _bearer(_api_key(session_factory, clock))
    for _ in range(3):
        client.get("/v1/payments", headers=headers)

    # Act
    response = client.get(path, headers=headers)

    # Assert
    assert response.status_code == 200
