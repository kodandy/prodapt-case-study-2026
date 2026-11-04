"""Route-level test for RT-001 of docs/specs/llm-adapter.md: a hostile 200 body never breaks a route."""

import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.__tests__.fake_openai import FakeOpenAiServer, ScriptedReply, fake_server_session
from ops_ai.assistant.openai_llm import OpenAiLlm
from ops_ai.ingest.consumer import apply_payment_event
from pydantic import JsonValue, SecretStr
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

CANARY_KEY = "sk-canary-openai-redteam-routes-1d84"
CHAT_MODEL = "chat-model-2026-01-01"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
DEEP_ARRAY = b"[" * 20000

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


@pytest.fixture(scope="module")
def key_pair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def _seed_declined_payment(session_factory: sessionmaker[Session]) -> uuid.UUID:
    payment_id = uuid.uuid4()
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=TEST_NOW - timedelta(minutes=5),
        entity_version=1,
        payment_id=payment_id,
        merchant_id=MERCHANT,
        amount_minor=1200,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
    )
    with session_factory.begin() as session:
        apply_payment_event(session, lambda: TEST_NOW, event)
    return payment_id


def _merchant_token(private_pem: str) -> str:
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
    return jwt.encode(claims, private_pem, algorithm="RS256")


def test_ask_route_answers_200_t2_fallback_when_openai_returns_a_deeply_nested_200_body(
    serve: ServeFake, session_factory: sessionmaker[Session], key_pair: tuple[str, str]
) -> None:
    # Arrange
    server = serve([ScriptedReply(raw_body=DEEP_ARRAY)])
    llm = OpenAiLlm(
        api_key=SecretStr(CANARY_KEY),
        chat_model=CHAT_MODEL,
        fallback_model=None,
        base_url=server.base_url,
        budget_seconds=5.0,
    )
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=llm,
        jwt_public_key_pem=key_pair[1],
        clock=lambda: TEST_NOW,
    )
    client = TestClient(create_app(deps))
    declined_id = _seed_declined_payment(session_factory)
    headers = {"Authorization": f"Bearer {_merchant_token(key_pair[0])}"}

    # Act
    response = client.post("/v1/assistant/ask", json={"question": "declined payments"}, headers=headers)

    # Assert
    body: JsonValue = response.json()
    assert response.status_code == 200
    assert isinstance(body, dict)
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert [item["ref"] for item in evidence if isinstance(item, dict)] == [
        "aggregate:decline_code:card_declined",
        f"payment:{declined_id}",
    ]
    assert len(server.requests) == 1
