"""Generated route x role authorization matrix, from docs/specs/compliance-rbac.md section 1.

Every route the app registers must have a ROUTE_ACTIONS entry, every ROUTE_ACTIONS entry must be registered,
and every non-public entry is exercised with a credential of each of the five roles: an allowed role must get
past the credential check; a denied role holding the same kind of credential as the allowed roles (consumer
session, merchant API key, ops session) must be answered exactly 403, and one holding another kind exactly
401.
"""

import re
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Literal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from gateway.accounts.rbac import MERCHANT_ROLES, OPS_ROLES, POLICY, PUBLIC, ROLES, ROUTE_ACTIONS, Role
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

CredentialKind = Literal["consumer_session", "merchant_api_key", "ops_session"]

MATRIX: list[tuple[str, Role]] = [
    (key, role) for key, action in sorted(ROUTE_ACTIONS.items()) if action != PUBLIC for role in ROLES
]
MATRIX_IDS = [f"{key}|{role}" for key, role in MATRIX]
NON_PUBLIC_KEYS = sorted(key for key, action in ROUTE_ACTIONS.items() if action != PUBLIC)


def credential_kind(role: Role) -> CredentialKind:
    """The bearer credential a role authenticates with (docs/specs/compliance-rbac.md section 1)."""
    if role in MERCHANT_ROLES:
        return "merchant_api_key"
    if role in OPS_ROLES:
        return "ops_session"
    return "consumer_session"


def route_kinds(key: str) -> set[CredentialKind]:
    return {credential_kind(role) for role in POLICY[ROUTE_ACTIONS[key]]}


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
        network_token="matrix-network-token",
        ops_admin_token="matrix-ops-admin-token",
        demo_top_up_enabled=True,
    )
    app = create_app(deps)
    client = TestClient(app, raise_server_exceptions=False)
    return MatrixApp(
        app=app, client=client, session_factory=session_factory, clock=clock, private_pem=private_pem
    )


def _api_routes(routes: Sequence[object], prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Every APIRoute with its full path. FastAPI 0.162 keeps an included router as one lazy entry in
    `app.routes` that holds the original APIRouter, so included routers are walked recursively."""
    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        router = getattr(route, "original_router", None)
        if isinstance(router, APIRouter):
            context_prefix = getattr(getattr(route, "include_context", None), "prefix", "")
            nested_prefix = context_prefix if isinstance(context_prefix, str) else ""
            yield from _api_routes(router.routes, prefix + nested_prefix)


def _registered_keys(app: FastAPI) -> set[str]:
    return {
        f"{method} {path}"
        for path, route in _api_routes(app.routes)
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


def test_every_route_action_entry_is_registered(matrix_app: MatrixApp) -> None:
    # Arrange
    registered = _registered_keys(matrix_app.app)

    # Act
    stale = sorted(key for key in ROUTE_ACTIONS if key not in registered)

    # Assert
    assert stale == []


@pytest.mark.parametrize("key", NON_PUBLIC_KEYS)
def test_every_allowed_role_of_a_route_uses_one_credential_kind(key: str) -> None:
    # Arrange
    allowed = POLICY[ROUTE_ACTIONS[key]]

    # Act
    kinds = route_kinds(key)

    # Assert
    assert len(allowed) >= 1
    assert len(kinds) == 1


@pytest.mark.parametrize(("key", "role"), MATRIX, ids=MATRIX_IDS)
def test_route_answers_each_role_according_to_the_policy(matrix_app: MatrixApp, key: str, role: Role) -> None:
    # Arrange
    assert key in _registered_keys(matrix_app.app), f"{key} is not registered by this app"
    method, template = key.split(" ", 1)
    path = PATH_PARAMETER.sub(lambda _match: str(uuid.uuid4()), template)
    token = _credential(matrix_app, role)
    headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "matrix-key"}

    # Act
    response = matrix_app.client.request(method, path, headers=headers, json={})

    # Assert
    if role in POLICY[ROUTE_ACTIONS[key]]:
        assert response.status_code not in (401, 403)
    elif credential_kind(role) in route_kinds(key):
        assert response.status_code == 403
    else:
        assert response.status_code == 401
