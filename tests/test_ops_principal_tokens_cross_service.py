"""Cross-service check: tokens minted by the gateway verify in ops-ai as the right principal.

Written from docs/specs/compliance-rbac.md sections 2 and 3. It lives in the repo-root `tests/` because the
service boundary rule forbids gateway sources, tests included, from importing ops_ai.
"""

import uuid
from dataclasses import dataclass

import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import issue_api_key
from gateway.admin import OpsRole, bootstrap_merchant, bootstrap_ops_user
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from ops_ai.auth import InvalidTokenError, verify_token
from ops_ai.scope import OpsScope, Scope
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
OPS_EMAIL = "ops@example.com"
OPS_PASSWORD = "ops-correct-horse-battery-9Z"  # noqa: S105 - test-only credential


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private_pem=private_pem, public_pem=public_pem)


def _client(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> TestClient:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(
        charge=charge,
        api_key_pepper=TEST_API_KEY_PEPPER,
        jwt_private_key_pem=private_pem,
        totp_encryption_key=TOTP_KEY,
    )
    return TestClient(create_app(deps))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ops_login(
    client: TestClient, session_factory: sessionmaker[Session], clock: FixedClock, role: OpsRole
) -> tuple[str, uuid.UUID]:
    """Enrols an ops user of `role` and logs in over HTTP; returns the ops session token and the user id."""
    enrolment = bootstrap_ops_user(session_factory, clock, OPS_EMAIL, OPS_PASSWORD, role, TOTP_KEY)
    code = pyotp.TOTP(enrolment.totp_secret).at(int(clock().timestamp()))
    response = client.post(
        "/v1/ops/sessions", json={"email": OPS_EMAIL, "password": OPS_PASSWORD, "totp_code": code}
    )
    assert response.status_code == 201
    return str(response.json()["access_token"]), enrolment.ops_user_id


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_token_from_the_ops_ai_token_route_verifies_in_ops_ai_as_an_ops_scope(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair, role: OpsRole
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    session_token, ops_user_id = _ops_login(client, session_factory, clock, role)
    response = client.post("/v1/ops/ops-ai-tokens", headers=_bearer(session_token))

    # Act
    principal = verify_token(response.json()["access_token"], key_pair.public_pem, clock())

    # Assert
    assert response.status_code == 201
    assert principal == OpsScope(ops_user_id=ops_user_id, role=role)
    assert type(principal) is OpsScope


def test_ops_session_token_is_not_accepted_by_ops_ai(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    session_token, _ = _ops_login(client, session_factory, clock, "ops_lead")

    # Act / Assert
    with pytest.raises(InvalidTokenError):
        verify_token(session_token, key_pair.public_pem, clock())


def test_merchant_token_for_a_support_key_verifies_in_ops_ai_as_a_support_scope(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    credentials = bootstrap_merchant(session_factory, clock, "Acme", TEST_API_KEY_PEPPER)
    with session_factory.begin() as session:
        support = issue_api_key(
            session, clock, credentials.merchant_id, TEST_API_KEY_PEPPER, role="merchant_support"
        )
    response = client.post("/v1/merchant-tokens", headers=_bearer(support.api_key))

    # Act
    principal = verify_token(response.json()["access_token"], key_pair.public_pem, clock())

    # Assert
    assert response.status_code == 201
    assert principal.merchant_id == credentials.merchant_id
    assert principal.role == "merchant_support"
    assert isinstance(principal.api_key_id, uuid.UUID)
    assert type(principal) is Scope


def test_merchant_token_for_an_admin_key_verifies_in_ops_ai_as_an_admin_scope(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    credentials = bootstrap_merchant(session_factory, clock, "Acme", TEST_API_KEY_PEPPER)
    response = client.post("/v1/merchant-tokens", headers=_bearer(credentials.api_key))

    # Act
    principal = verify_token(response.json()["access_token"], key_pair.public_pem, clock())

    # Assert
    assert response.status_code == 201
    assert type(principal) is Scope
    assert principal.merchant_id == credentials.merchant_id
    assert principal.role == "merchant_admin"
    assert isinstance(principal.api_key_id, uuid.UUID)
