"""Generated route x role authorization matrix, from docs/specs/compliance-rbac.md section 1.

Every route the app registers must have a ROUTE_ACTIONS entry, and every non-public entry is exercised with
a credential of each of the five roles: an allowed role must get past the credential check, every other role
must be answered 401 (wrong kind of credential) or 403 (right kind, role not allowed).
"""

import re
import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from gateway.accounts.rbac import POLICY, PUBLIC, ROLES, ROUTE_ACTIONS, Role
from gateway.accounts.service import issue_api_key, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.admin import OpsPrincipal, bootstrap_merchant, bootstrap_ops_user, issue_ops_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
PASSWORD = "matrix-password-correct-7Q"
SKIPPED_METHODS = frozenset({"HEAD", "OPTIONS"})
PATH_PARAMETER = re.compile(r"\{[^}]+\}")

MATRIX: list[tuple[str, Role]] = [
    (key, role) for key, action in sorted(ROUTE_ACTIONS.items()) if action != PUBLIC for role in ROLES
]
MATRIX_IDS = [f"{key}|{role}" for key, role in MATRIX]


@dataclass(frozen=True)
class MatrixApp:
    app: FastAPI
    client: TestClient
    session_factory: sessionmaker[Session]
    clock: FixedClock
    private_pem: str


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def matrix_app(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> MatrixApp:
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
    app = create_app(deps)
    client = TestClient(app, raise_server_exceptions=False)
    return MatrixApp(
        app=app, client=client, session_factory=session_factory, clock=clock, private_pem=private_pem
    )


def _registered_keys(app: FastAPI) -> set[str]:
    return {
        f"{method} {route.path}"
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if method not in SKIPPED_METHODS
    }


def _credential(matrix: MatrixApp, role: Role) -> str:
    """A bearer credential of the kind `role` uses: consumer session, merchant API key or ops session."""
    if role == "consumer":
        with matrix.session_factory() as session:
            user_id = sign_up(session, matrix.clock, "matrix-user@example.com", PASSWORD).user_id
            session.commit()
        return issue_session_token(matrix.private_pem, user_id, matrix.clock(), ("pwd",))
    if role == "merchant_admin":
        credentials = bootstrap_merchant(matrix.session_factory, matrix.clock, "Matrix", TEST_API_KEY_PEPPER)
        return credentials.api_key
    if role == "merchant_support":
        credentials = bootstrap_merchant(matrix.session_factory, matrix.clock, "Matrix", TEST_API_KEY_PEPPER)
        with matrix.session_factory.begin() as session:
            return issue_api_key(
                session, matrix.clock, credentials.merchant_id, TEST_API_KEY_PEPPER, role="merchant_support"
            ).api_key
    enrolment = bootstrap_ops_user(
        matrix.session_factory, matrix.clock, "matrix-ops@example.com", PASSWORD, role, TOTP_KEY
    )
    principal = OpsPrincipal(ops_user_id=enrolment.ops_user_id, role=role)
    return issue_ops_session_token(matrix.private_pem, principal, matrix.clock())


def test_every_registered_route_has_a_route_action_entry(matrix_app: MatrixApp) -> None:
    # Arrange
    registered = _registered_keys(matrix_app.app)

    # Act
    unmapped = sorted(key for key in registered if key not in ROUTE_ACTIONS)

    # Assert
    assert unmapped == []


@pytest.mark.parametrize(("key", "role"), MATRIX, ids=MATRIX_IDS)
def test_route_answers_each_role_according_to_the_policy(matrix_app: MatrixApp, key: str, role: Role) -> None:
    # Arrange
    if key not in _registered_keys(matrix_app.app):
        pytest.skip(f"{key} is not registered by this app")
    method, template = key.split(" ", 1)
    path = PATH_PARAMETER.sub(lambda _match: str(uuid.uuid4()), template)
    token = _credential(matrix_app, role)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "matrix-key"}

    # Act
    response = matrix_app.client.request(method, path, headers=headers, json={})

    # Assert
    if role in POLICY[ROUTE_ACTIONS[key]]:
        assert response.status_code not in (401, 403)
    else:
        assert response.status_code in (401, 403)
