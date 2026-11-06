"""R2-7: a merchant token names the API key that obtained it, so ops-ai can record the acting key."""

import uuid
from datetime import UTC, datetime

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.models import ApiKey
from gateway.accounts.service import authenticate_api_key_principal, create_merchant
from gateway.accounts.tokens import issue_merchant_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
API_KEY_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9cff")


def _private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _claims(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def _merchant_with_key_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[uuid.UUID, str, uuid.UUID]:
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER, role="merchant_support")
        session.commit()
        key_id = session.scalar(select(ApiKey.id).where(ApiKey.merchant_id == credentials.merchant_id))
    assert key_id is not None
    return credentials.merchant_id, credentials.api_key, key_id


def test_issue_merchant_token_with_an_api_key_id_carries_it_as_a_claim() -> None:
    # Arrange
    pem = _private_pem()

    # Act
    token = issue_merchant_token(pem, MERCHANT_ID, NOW, api_key_id=API_KEY_ID)

    # Assert
    assert _claims(token)["api_key_id"] == str(API_KEY_ID)


def test_issue_merchant_token_without_an_api_key_id_has_no_such_claim() -> None:
    # Arrange
    pem = _private_pem()

    # Act
    token = issue_merchant_token(pem, MERCHANT_ID, NOW)

    # Assert
    assert "api_key_id" not in _claims(token)


def test_authenticate_api_key_principal_returns_the_merchant_role_and_key_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    merchant_id, api_key, key_id = _merchant_with_key_id(session_factory, clock)

    # Act
    with session_factory() as session:
        principal = authenticate_api_key_principal(session, api_key, TEST_API_KEY_PEPPER)

    # Assert
    assert principal is not None
    assert (principal.merchant_id, principal.role, principal.api_key_id) == (
        merchant_id,
        "merchant_support",
        key_id,
    )


def test_authenticate_api_key_principal_refuses_an_unknown_key(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    unknown = "sk_test_unknown"

    # Act
    with session_factory() as session:
        principal = authenticate_api_key_principal(session, unknown, TEST_API_KEY_PEPPER)

    # Assert
    assert principal is None


def test_merchant_tokens_route_names_the_api_key_that_requested_the_token(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    app_deps = GatewayAppDeps(
        charge=deps, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=_private_pem()
    )
    client = TestClient(create_app(app_deps))
    merchant_id, api_key, key_id = _merchant_with_key_id(session_factory, clock)

    # Act
    response = client.post("/v1/merchant-tokens", headers={"Authorization": f"Bearer {api_key}"})

    # Assert
    claims = _claims(response.json()["access_token"])
    assert response.status_code == 201
    assert (claims["merchant_id"], claims["roles"], claims["api_key_id"]) == (
        str(merchant_id),
        ["merchant_support"],
        str(key_id),
    )
