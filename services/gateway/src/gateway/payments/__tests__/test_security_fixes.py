"""RED tests for the post-review security fixes, from docs/specs/disputes-notifications.md and runtime.md:
the `user:` Idempotency-Key namespace is reserved, an empty network / ops token means "not configured",
the gateway refuses to start when both tokens are equal, and `lock_payment` can be scoped to the payer.
"""

import re
import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.main import build_app_from_env
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.refunds import lock_payment
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
PASSWORD = "security-fixes-password-7Q"
INVALID_KEY_BODY = {"detail": "invalid Idempotency-Key header"}
NETWORK_NOT_CONFIGURED = {"detail": "network not configured"}
OPS_NOT_CONFIGURED = {"detail": "ops not configured"}
UNKNOWN_ID = "00000000-0000-4000-8000-000000000001"
SHARED_TOKEN = "shared-token-value-0123456789-abcdef"
NETWORK_ONLY_TOKEN = "network-token-value-0123456789-abcdef"
OPS_ONLY_TOKEN = "ops-admin-token-value-0123456789-abc"
PEPPER = "pepper-for-startup-checks-0123456789"
FINGERPRINT_KEY = "fingerprint-for-startup-checks-0123456789"
DUMMY_DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
DUMMY_REDIS_URL = "redis://localhost:6379/0"


@dataclass(frozen=True)
class Env:
    deps: ChargeDeps
    client: TestClient
    private_pem: str
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    merchant_auth: dict[str, str]


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _env(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    network_token: str | None = None,
    ops_admin_token: str | None = None,
) -> Env:
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
    app = create_app(
        GatewayAppDeps(
            charge=deps,
            api_key_pepper=TEST_API_KEY_PEPPER,
            jwt_private_key_pem=private_pem,
            network_token=network_token,
            ops_admin_token=ops_admin_token,
        )
    )
    return Env(
        deps,
        TestClient(app),
        private_pem,
        session_factory,
        clock,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
    )


def _add_user(env: Env, email: str) -> uuid.UUID:
    with env.session_factory() as session:
        result = sign_up(session, env.clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _user_auth(env: Env, user_id: uuid.UUID) -> dict[str, str]:
    token = issue_session_token(env.private_pem, user_id, env.clock(), ("pwd",))
    return {"Authorization": f"Bearer {token}"}


def _card() -> dict[str, object]:
    return {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"}


def _merchant_charge(env: Env, idempotency_key: str) -> Response:
    response: Response = env.client.post(
        "/v1/payments",
        json={"amount_minor": 1500, "currency": "USD", "method": "card", "card": _card()},
        headers={**env.merchant_auth, "Idempotency-Key": idempotency_key},
    )
    return response


def _paid_by(env: Env, user_id: uuid.UUID) -> uuid.UUID:
    response: Response = env.client.post(
        "/v1/me/payments",
        json={
            "merchant_id": str(env.merchant_id),
            "amount_minor": 1500,
            "currency": "USD",
            "card": _card(),
        },
        headers={**_user_auth(env, user_id), "Idempotency-Key": uuid.uuid4().hex},
    )
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    return uuid.UUID(str(response.json()["payment_id"]))


def _payment_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.payments")).scalar_one())


# --- POST /v1/payments reserves the `user:` Idempotency-Key namespace ------------------------------------


@pytest.mark.parametrize("key", ["user:", "user:abc", f"user:{UNKNOWN_ID}:abc"])
def test_merchant_charge_with_a_user_prefixed_idempotency_key_returns_400_and_creates_no_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, key: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _merchant_charge(env, key)

    # Assert
    assert response.status_code == 400
    assert response.json() == INVALID_KEY_BODY
    assert _payment_count(session_factory) == 0


def test_merchant_charge_with_an_ordinary_idempotency_key_still_returns_201_and_creates_one_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _merchant_charge(env, "ordinary-key-1")

    # Assert
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    assert _payment_count(session_factory) == 1


# --- empty network / ops token means "not configured" ----------------------------------------------------


@pytest.mark.parametrize(
    "presented",
    [{}, {"X-Network-Token": ""}],
    ids=["no_header", "empty_header"],
)
@pytest.mark.parametrize("route", ["open", "resolution"])
def test_network_routes_with_an_empty_configured_token_return_503_not_configured(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    route: str,
    presented: dict[str, str],
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, network_token="", ops_admin_token=OPS_ONLY_TOKEN)
    if route == "open":
        path, body = "/v1/network/disputes", {"payment_id": UNKNOWN_ID, "reason": "fraudulent"}
    else:
        path, body = f"/v1/network/disputes/{UNKNOWN_ID}/resolution", {"outcome": "won"}

    # Act
    response = env.client.post(path, json=body, headers=presented)

    # Assert
    assert response.status_code == 503
    assert response.json() == NETWORK_NOT_CONFIGURED


@pytest.mark.parametrize(
    "presented",
    [{}, {"X-Ops-Admin-Token": ""}],
    ids=["no_header", "empty_header"],
)
def test_ops_resolution_with_an_empty_configured_token_returns_503_not_configured(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    presented: dict[str, str],
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, network_token=NETWORK_ONLY_TOKEN, ops_admin_token="")

    # Act
    response = env.client.post(
        f"/v1/ops/disputes/{UNKNOWN_ID}/resolution", json={"outcome": "won"}, headers=presented
    )

    # Assert
    assert response.status_code == 503
    assert response.json() == OPS_NOT_CONFIGURED


def test_a_wrong_token_on_a_configured_ops_route_still_returns_401_not_503(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, network_token="", ops_admin_token=OPS_ONLY_TOKEN)

    # Act
    response = env.client.post(
        f"/v1/ops/disputes/{UNKNOWN_ID}/resolution",
        json={"outcome": "won"},
        headers={"X-Ops-Admin-Token": ""},
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid ops token"}


# --- startup refuses equal network / ops tokens -----------------------------------------------------------


def _startup_env(monkeypatch: pytest.MonkeyPatch, private_pem: str, network: str, ops: str) -> None:
    monkeypatch.delenv("GATEWAY_SECRETS_DIR", raising=False)
    monkeypatch.setenv("DATABASE_URL", DUMMY_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", DUMMY_REDIS_URL)
    monkeypatch.setenv("GATEWAY_API_KEY_PEPPER", PEPPER)
    monkeypatch.setenv("GATEWAY_CARD_FINGERPRINT_KEY", FINGERPRINT_KEY)
    monkeypatch.setenv("GATEWAY_JWT_PRIVATE_KEY_PEM", private_pem)
    monkeypatch.setenv("GATEWAY_NETWORK_TOKEN", network)
    monkeypatch.setenv("GATEWAY_OPS_ADMIN_TOKEN", ops)


def test_build_app_from_env_with_equal_network_and_ops_tokens_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    # Arrange
    _startup_env(monkeypatch, private_pem, network=SHARED_TOKEN, ops=SHARED_TOKEN)

    # Act
    with pytest.raises(RuntimeError) as exc:
        build_app_from_env()

    # Assert
    assert re.search(r"GATEWAY_NETWORK_TOKEN and GATEWAY_OPS_ADMIN_TOKEN must differ", str(exc.value))
    assert str(exc.value) == "GATEWAY_NETWORK_TOKEN and GATEWAY_OPS_ADMIN_TOKEN must differ"


def test_build_app_from_env_with_distinct_network_and_ops_tokens_builds_the_app(
    monkeypatch: pytest.MonkeyPatch, private_pem: str
) -> None:
    # Arrange
    _startup_env(monkeypatch, private_pem, network=NETWORK_ONLY_TOKEN, ops=OPS_ONLY_TOKEN)

    # Act
    app = build_app_from_env()

    # Assert
    assert isinstance(app, FastAPI)
    assert app.title == "gateway"


# --- lock_payment scoped to the payer ---------------------------------------------------------------------


def test_lock_payment_scoped_to_a_different_user_returns_none(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payer = _add_user(env, "payer@example.com")
    other = _add_user(env, "other@example.com")
    payment_id = _paid_by(env, payer)

    # Act
    with session_factory() as session:
        locked = lock_payment(session, payment_id, payer_user_id=other)

    # Assert
    assert locked is None


def test_lock_payment_scoped_to_the_payer_returns_that_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payer = _add_user(env, "payer@example.com")
    payment_id = _paid_by(env, payer)

    # Act
    with session_factory() as session:
        locked = lock_payment(session, payment_id, payer_user_id=payer)
        locked_id = None if locked is None else locked.id
        locked_payer = None if locked is None else locked.payer_user_id

    # Assert
    assert locked_id == payment_id
    assert locked_payer == payer
