"""Tests for POST /v1/user-tokens, written from docs/specs/sq2.md section 1 (gateway/app.py)."""

import uuid
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

ROUTE = "/v1/user-tokens"
EMAIL = "sq2-sender@example.com"
PASSWORD = "route-test-password-7Q"
INVALID_CREDENTIALS = {"detail": "invalid credentials"}


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _public_key_pem(private_key_pem: str) -> str:
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _client(
    session_factory: sessionmaker[Session], clock: FixedClock, *, jwt_private_key_pem: str | None
) -> TestClient:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(
        charge=charge, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=jwt_private_key_pem
    )
    return TestClient(create_app(deps))


def _sign_up(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL, password: str = PASSWORD
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, password)
        session.commit()
    return result.user_id


def _unverified_claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


# --- success ---------------------------------------------------------------------------------------------


def test_user_tokens_returns_201_with_a_bearer_token_that_expires_in_900_seconds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    private_key_pem = _private_key_pem()
    client = _client(session_factory, clock, jwt_private_key_pem=private_key_pem)
    user_id = _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD})

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {"access_token": body.get("access_token"), "token_type": "Bearer", "expires_in": 900}
    claims = jwt.decode(
        body["access_token"],
        _public_key_pem(private_key_pem),
        algorithms=["RS256"],
        audience="ops-ai",
        issuer="gateway",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["user_id"] == str(user_id)
    assert claims["sub"] == f"user:{user_id}"
    assert claims["roles"] == ["consumer"]
    assert "merchant_id" not in claims
    assert claims["iat"] == int(clock().timestamp())
    assert claims["exp"] == int(clock().timestamp()) + 900


def test_user_tokens_201_response_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD})

    # Assert
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"


def test_user_tokens_matches_the_email_case_insensitively(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    user_id = _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": "SQ2-Sender@Example.COM", "password": PASSWORD})

    # Assert
    assert response.status_code == 201
    assert _unverified_claims(response.json()["access_token"])["user_id"] == str(user_id)


def test_user_tokens_issues_the_token_for_the_user_that_owns_the_credentials(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    alice = _sign_up(session_factory, clock, email="alice@example.com", password="alice-password-1")
    bob = _sign_up(session_factory, clock, email="bob@example.com", password="bob-password-22")

    # Act
    token_a = client.post(ROUTE, json={"email": "alice@example.com", "password": "alice-password-1"})
    token_b = client.post(ROUTE, json={"email": "bob@example.com", "password": "bob-password-22"})

    # Assert
    assert alice != bob
    assert _unverified_claims(token_a.json()["access_token"])["user_id"] == str(alice)
    assert _unverified_claims(token_b.json()["access_token"])["user_id"] == str(bob)


def test_user_tokens_issues_a_distinct_token_on_each_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    _sign_up(session_factory, clock)

    # Act
    first = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD}).json()["access_token"]
    second = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD}).json()["access_token"]

    # Assert
    assert _unverified_claims(first)["jti"] != _unverified_claims(second)["jti"]


# --- failed login ----------------------------------------------------------------------------------------


def test_user_tokens_with_an_unknown_email_returns_401_invalid_credentials(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": "nobody@example.com", "password": PASSWORD})

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS


def test_user_tokens_with_a_wrong_password_returns_401_invalid_credentials_and_no_token(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": "wrong-password-canary-3917"})

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS
    assert "access_token" not in response.text
    assert "wrong-password-canary-3917" not in response.text


def test_user_tokens_gives_unknown_email_and_wrong_password_the_identical_response(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    _sign_up(session_factory, clock)

    # Act
    unknown_email = client.post(ROUTE, json={"email": "nobody@example.com", "password": PASSWORD})
    wrong_password = client.post(ROUTE, json={"email": EMAIL, "password": "some other password"})

    # Assert
    assert unknown_email.status_code == wrong_password.status_code == 401
    assert unknown_email.content == wrong_password.content
    assert unknown_email.headers["content-type"] == wrong_password.headers["content-type"]


# --- validation ------------------------------------------------------------------------------------------


def test_user_tokens_with_an_extra_field_returns_422_and_issues_no_token(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    _sign_up(session_factory, clock)
    body = {"email": EMAIL, "password": PASSWORD, "user_id": str(uuid.uuid4())}

    # Act
    response = client.post(ROUTE, json=body)

    # Assert
    assert response.status_code == 422
    assert "access_token" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": EMAIL},
        {"password": "valid-password-canary-1"},
        {"email": "not-an-email", "password": "valid-password-canary-2"},
        {"email": "ab", "password": "valid-password-canary-3"},
        {"email": "canary-short-pw@example.com", "password": "short7!"},
        {"email": "canary-long-pw@example.com", "password": "p" * 257},
        {"email": "canary-extra@example.com", "password": "valid-password-canary-4", "role": "admin"},
        {"email": 123, "password": "valid-password-canary-5"},
        {"email": "canary-nonstr@example.com", "password": 12345678},
    ],
    ids=[
        "empty_body",
        "no_password",
        "no_email",
        "email_without_at",
        "email_too_short",
        "password_too_short",
        "password_too_long",
        "extra_field",
        "email_not_a_string",
        "password_not_a_string",
    ],
)
def test_user_tokens_rejects_an_invalid_body_with_422_that_never_echoes_the_submitted_values(
    session_factory: sessionmaker[Session], clock: FixedClock, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=_private_key_pem())
    submitted = [value for value in body.values() if isinstance(value, str) and len(value) >= 6]

    # Act
    response = client.post(ROUTE, json=body)

    # Assert
    assert response.status_code == 422
    assert [value for value in submitted if value in response.text] == []


# --- configuration ---------------------------------------------------------------------------------------


def test_user_tokens_without_a_configured_private_key_returns_503(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, jwt_private_key_pem=None)
    _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD})

    # Assert
    assert response.status_code == 503
    assert response.json() == {"detail": "token issuing not configured"}
