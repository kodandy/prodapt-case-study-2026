"""Tests for the ops and merchant-support principals of ops_ai.auth and ops_ai.scope, written from
docs/specs/compliance-rbac.md section 3."""

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.auth import InvalidTokenError, verify_token
from ops_ai.scope import (
    OPS_ROLES,
    ForbiddenPrincipalError,
    OpsScope,
    Scope,
    UserScope,
    require_ops_role,
)
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ISSUED_AT = int(NOW.timestamp())
OPS_USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c91")
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
USER_ID = uuid.UUID("5d2c8e41-7a39-4b60-8f12-3c4d5e6f7a88")
FORBIDDEN_BODY = {"detail": "forbidden"}
QUESTION = "what happened to my declined payments?"


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


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text="narration", model="recording-model-1")


def _ops_claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"ops:{OPS_USER_ID}",
        "ops_user_id": str(OPS_USER_ID),
        "roles": ["ops_lead"],
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return claims


def _merchant_claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT_ID}",
        "merchant_id": str(MERCHANT_ID),
        "roles": ["merchant_admin"],
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return claims


def _sign(claims: dict[str, object], key_pair: KeyPair) -> str:
    return jwt.encode(claims, key_pair.private_pem, algorithm="RS256")


# --- OpsScope --------------------------------------------------------------------------------------------


def test_ops_roles_are_ops_agent_and_ops_lead() -> None:
    # Arrange / Act
    roles = frozenset(OPS_ROLES)

    # Assert
    assert roles == frozenset({"ops_agent", "ops_lead"})


def test_ops_scope_carries_the_ops_user_id_and_role_and_is_frozen() -> None:
    # Arrange
    scope = OpsScope(ops_user_id=OPS_USER_ID, role="ops_lead")

    # Act
    with pytest.raises(dataclasses.FrozenInstanceError) as frozen:
        scope.role = "ops_agent"  # type: ignore[misc]

    # Assert
    assert (scope.ops_user_id, scope.role) == (OPS_USER_ID, "ops_lead")
    assert scope == OpsScope(ops_user_id=OPS_USER_ID, role="ops_lead")
    assert scope != OpsScope(ops_user_id=OPS_USER_ID, role="ops_agent")
    assert frozen.type is dataclasses.FrozenInstanceError


def test_scope_defaults_to_the_merchant_admin_role() -> None:
    # Arrange / Act
    scope = Scope(merchant_id=MERCHANT_ID)

    # Assert
    assert scope.role == "merchant_admin"
    assert scope == Scope(merchant_id=MERCHANT_ID, role="merchant_admin")
    assert scope != Scope(merchant_id=MERCHANT_ID, role="merchant_support")


# --- verify_token: ops tokens ------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_verify_token_returns_an_ops_scope_for_an_ops_token(key_pair: KeyPair, role: str) -> None:
    # Arrange
    token = _sign(_ops_claims(roles=[role]), key_pair)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == OpsScope(ops_user_id=OPS_USER_ID, role=role)  # type: ignore[arg-type]
    assert type(principal) is OpsScope


@pytest.mark.parametrize(
    "overrides",
    [
        {"roles": ["ops_admin"]},
        {"roles": ["ops_agent", "ops_lead"]},
        {"roles": []},
        {"roles": "ops_lead"},
        {"roles": ["consumer"]},
        {"roles": ["merchant_admin"]},
        {"sub": f"ops:{uuid.UUID('9e8d7c6b-5a49-4382-b1a0-0f1e2d3c4b5a')}"},
        {"sub": f"user:{OPS_USER_ID}"},
        {"sub": f"merchant:{OPS_USER_ID}"},
        {"sub": "ops:"},
        {"ops_user_id": str(OPS_USER_ID).upper(), "sub": f"ops:{str(OPS_USER_ID).upper()}"},
        {"ops_user_id": "not-a-uuid", "sub": "ops:not-a-uuid"},
        {"ops_user_id": ""},
        {"ops_user_id": 7},
        {"merchant_id": str(MERCHANT_ID)},
        {"user_id": str(USER_ID)},
    ],
    ids=[
        "unknown_ops_role",
        "two_ops_roles",
        "no_roles",
        "roles_not_a_list",
        "consumer_role",
        "merchant_role",
        "sub_names_another_ops_user",
        "sub_is_a_user",
        "sub_is_a_merchant",
        "sub_without_id",
        "uppercase_uuid",
        "non_uuid",
        "empty_id",
        "non_string_id",
        "ops_and_merchant_id",
        "ops_and_user_id",
    ],
)
def test_verify_token_rejects_an_invalid_ops_token(key_pair: KeyPair, overrides: dict[str, object]) -> None:
    # Arrange
    token = _sign(_ops_claims(**overrides), key_pair)

    # Act / Assert
    with pytest.raises(InvalidTokenError):
        verify_token(token, key_pair.public_pem, NOW)


def test_verify_token_rejects_an_ops_token_without_a_sub(key_pair: KeyPair) -> None:
    # Arrange
    claims = _ops_claims()
    del claims["sub"]

    # Act / Assert
    with pytest.raises(InvalidTokenError):
        verify_token(_sign(claims, key_pair), key_pair.public_pem, NOW)


def test_verify_token_rejects_an_expired_ops_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_ops_claims(), key_pair)

    # Act / Assert
    with pytest.raises(InvalidTokenError):
        verify_token(token, key_pair.public_pem, datetime.fromtimestamp(ISSUED_AT + 900, UTC))


def test_verify_token_rejects_an_ops_token_that_lives_longer_than_fifteen_minutes(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_ops_claims(exp=ISSUED_AT + 901), key_pair)

    # Act / Assert
    with pytest.raises(InvalidTokenError):
        verify_token(token, key_pair.public_pem, NOW)


# --- verify_token: merchant roles --------------------------------------------------------------------------


def test_verify_token_returns_a_support_scope_for_a_merchant_support_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(roles=["merchant_support"]), key_pair)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == Scope(merchant_id=MERCHANT_ID, role="merchant_support")
    assert type(principal) is Scope


def test_verify_token_returns_an_admin_scope_for_a_merchant_admin_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(), key_pair)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == Scope(merchant_id=MERCHANT_ID, role="merchant_admin")


@pytest.mark.parametrize(
    "roles",
    [
        ["merchant_admin", "merchant_support"],
        ["merchant_support", "merchant_admin"],
        ["merchant_support", "merchant_support"],
        ["ops_lead"],
        ["consumer"],
        [],
    ],
)
def test_verify_token_rejects_a_merchant_token_whose_roles_are_not_exactly_one_merchant_role(
    key_pair: KeyPair, roles: list[str]
) -> None:
    # Arrange
    token = _sign(_merchant_claims(roles=roles), key_pair)

    # Act / Assert
    with pytest.raises(InvalidTokenError):
        verify_token(token, key_pair.public_pem, NOW)


# --- require_ops_role --------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_require_ops_role_returns_an_ops_scope_for_any_ops_role_by_default(role: str) -> None:
    # Arrange
    principal = OpsScope(ops_user_id=OPS_USER_ID, role=role)  # type: ignore[arg-type]

    # Act
    result = require_ops_role(principal)

    # Assert
    assert result == principal


def test_require_ops_role_accepts_a_role_inside_the_allowed_set() -> None:
    # Arrange
    principal = OpsScope(ops_user_id=OPS_USER_ID, role="ops_lead")

    # Act
    result = require_ops_role(principal, allowed=frozenset({"ops_lead"}))

    # Assert
    assert result == principal


def test_require_ops_role_rejects_an_ops_role_outside_the_allowed_set() -> None:
    # Arrange
    principal = OpsScope(ops_user_id=OPS_USER_ID, role="ops_agent")

    # Act / Assert
    with pytest.raises(ForbiddenPrincipalError):
        require_ops_role(principal, allowed=frozenset({"ops_lead"}))


@pytest.mark.parametrize(
    "principal",
    [
        Scope(merchant_id=MERCHANT_ID),
        Scope(merchant_id=MERCHANT_ID, role="merchant_support"),
        UserScope(user_id=USER_ID),
    ],
    ids=["merchant_admin", "merchant_support", "user"],
)
def test_require_ops_role_rejects_a_non_ops_principal(principal: Scope | UserScope) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ForbiddenPrincipalError):
        require_ops_role(principal)


# --- ops tokens on the existing ops-ai routes --------------------------------------------------------------


def _client(session_factory: sessionmaker[Session], public_pem: str, llm: RecordingLlm) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=llm, jwt_public_key_pem=public_pem, clock=lambda: TEST_NOW
    )
    return TestClient(create_app(deps))


def _ops_token_now(key_pair: KeyPair) -> str:
    issued_at = int(TEST_NOW.timestamp())
    return _sign(_ops_claims(iat=issued_at, exp=issued_at + 900), key_pair)


def test_a_merchant_route_answers_an_ops_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, key_pair.public_pem, llm)

    # Act
    response = client.post(
        "/v1/assistant/ask",
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {_ops_token_now(key_pair)}"},
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


def test_a_user_route_answers_an_ops_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, key_pair.public_pem, llm)

    # Act
    response = client.post(
        "/v1/assistant/investigate-transfer",
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {_ops_token_now(key_pair)}"},
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


def test_a_merchant_route_still_accepts_a_merchant_support_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    issued_at = int(TEST_NOW.timestamp())
    token = _sign(_merchant_claims(roles=["merchant_support"], iat=issued_at, exp=issued_at + 900), key_pair)

    # Act
    response = client.post(
        "/v1/assistant/ask", json={"question": QUESTION}, headers={"Authorization": f"Bearer {token}"}
    )

    # Assert
    assert response.status_code == 200
