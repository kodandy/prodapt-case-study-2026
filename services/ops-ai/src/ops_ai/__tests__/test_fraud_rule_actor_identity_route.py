"""R2-7: the revert route records the API key named by the verified token, never one from the body."""

import uuid

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

REVERT_ROUTE = "/v1/feedback/fraud-rules/revert"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
API_KEY = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9cff")
FORGED_KEY = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9cee")
SELECT_IDENTITY = text(
    "SELECT version, actor_kind, actor_id FROM ops_ai.fraud_rule_version_actors WHERE merchant_id = :merchant"
)


class IdleLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise AssertionError(f"revert must not narrate: {request}")


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _client(session_factory: sessionmaker[Session], private_pem: str) -> TestClient:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=IdleLlm(), jwt_public_key_pem=public_pem, clock=lambda: TEST_NOW
    )
    return TestClient(create_app(deps))


def _headers(private_pem: str, api_key_id: uuid.UUID | None) -> dict[str, str]:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT}",
        "merchant_id": str(MERCHANT),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    if api_key_id is not None:
        claims["api_key_id"] = str(api_key_id)
    return {"Authorization": f"Bearer {jwt.encode(claims, private_pem, algorithm='RS256')}"}


def _stored(session_factory: sessionmaker[Session]) -> list[tuple[object, ...]]:
    with session_factory() as session:
        return [tuple(row) for row in session.execute(SELECT_IDENTITY, {"merchant": MERCHANT})]


def test_a_revert_with_a_keyed_token_records_the_tokens_api_key(
    session_factory: sessionmaker[Session], private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, private_pem)

    # Act
    response = client.post(REVERT_ROUTE, json={"version": 0}, headers=_headers(private_pem, API_KEY))

    # Assert
    assert response.status_code == 201
    assert _stored(session_factory) == [(1, "api_key", API_KEY)]


def test_a_revert_with_a_token_without_a_key_records_the_merchant_principal_without_an_id(
    session_factory: sessionmaker[Session], private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, private_pem)

    # Act
    response = client.post(REVERT_ROUTE, json={"version": 0}, headers=_headers(private_pem, None))

    # Assert
    assert response.status_code == 201
    assert _stored(session_factory) == [(1, "merchant_principal", None)]


def test_a_revert_body_naming_an_api_key_is_refused_and_writes_nothing(
    session_factory: sessionmaker[Session], private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, private_pem)
    body = {"version": 0, "api_key_id": str(FORGED_KEY)}

    # Act
    response = client.post(REVERT_ROUTE, json=body, headers=_headers(private_pem, API_KEY))

    # Assert
    assert response.status_code == 422
    assert _stored(session_factory) == []


def test_a_revert_response_keeps_the_existing_shape_without_actor_identity_fields(
    session_factory: sessionmaker[Session], private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, private_pem)

    # Act
    response = client.post(REVERT_ROUTE, json={"version": 0}, headers=_headers(private_pem, API_KEY))

    # Assert
    assert set(response.json()) == {
        "version",
        "steps",
        "min_micro_payments",
        "min_micro_share",
        "min_distinct_cards",
        "reason",
        "feedback_id",
        "created_at",
    }
