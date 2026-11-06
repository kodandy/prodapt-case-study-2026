"""Route wiring of the per-tenant agent streams, written from docs/specs/r2-agents.md section 4.3."""

import uuid
from dataclasses import dataclass, field

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.agents.bus import read_tenant_messages
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.scope import Scope, UserScope
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

INVESTIGATE = "/v1/agents/investigate"
RCA = "/v1/agents/transfer-rca"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9ca1")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9ca2")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-0000000000b1")
INSERT_TRANSFER = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, 180000, 'USD', 'bank_transfer', 'failed', 'US', 'IN', "
    "'cross_border_limit_kyc_tier', 'xb_limit_kyc_tier1_v3', 1, 100000, 'USD', 1000000, 525000, "
    ":decided_at, 'seed')"
)


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text="narration", model="recording-model")


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


@pytest.fixture
def client(session_factory: sessionmaker[Session], redis_client: Redis, key_pair: KeyPair) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=RecordingLlm(),
        jwt_public_key_pem=key_pair.public_pem,
        clock=lambda: TEST_NOW,
        redis=redis_client,
    )
    return TestClient(create_app(deps))


def _token(private_pem: str, subject: str, extra: dict[str, object], roles: list[str]) -> dict[str, str]:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, private_pem, algorithm='RS256')}"}


def _merchant_headers(key_pair: KeyPair, merchant_id: uuid.UUID) -> dict[str, str]:
    return _token(
        key_pair.private_pem, f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"]
    )


def _user_headers(key_pair: KeyPair, user_id: uuid.UUID) -> dict[str, str]:
    return _token(key_pair.private_pem, f"user:{user_id}", {"user_id": str(user_id)}, ["consumer"])


def _correlation_and_count(body: object) -> tuple[uuid.UUID, int]:
    assert isinstance(body, dict)
    messages = body["messages"]
    assert isinstance(messages, list)
    return uuid.UUID(str(body["correlation_id"])), len(messages)


def test_investigate_mirrors_its_trail_to_the_token_merchants_stream_only(
    client: TestClient, redis_client: Redis, key_pair: KeyPair
) -> None:
    # Act
    response = client.post(
        INVESTIGATE, json={"question": "Is this card testing?"}, headers=_merchant_headers(key_pair, MERCHANT)
    )

    # Assert
    assert response.status_code == 200
    correlation_id, count = _correlation_and_count(response.json())
    own = read_tenant_messages(redis_client, Scope(merchant_id=MERCHANT))
    assert [message.correlation_id for message in own] == [correlation_id] * count
    assert [message.sequence for message in own] == list(range(1, count + 1))
    assert read_tenant_messages(redis_client, Scope(merchant_id=OTHER_MERCHANT)) == []


def test_transfer_rca_mirrors_its_trail_to_the_token_users_stream_only(
    client: TestClient,
    redis_client: Redis,
    key_pair: KeyPair,
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        session.execute(
            INSERT_TRANSFER, {"transfer_id": uuid.uuid4(), "sender_user_id": SENDER, "decided_at": TEST_NOW}
        )

    # Act
    response = client.post(
        RCA, json={"question": "Why was my transfer declined?"}, headers=_user_headers(key_pair, SENDER)
    )

    # Assert
    assert response.status_code == 200
    correlation_id, count = _correlation_and_count(response.json())
    own = read_tenant_messages(redis_client, UserScope(user_id=SENDER))
    assert [message.correlation_id for message in own] == [correlation_id] * count
    assert read_tenant_messages(redis_client, Scope(merchant_id=SENDER)) == []
