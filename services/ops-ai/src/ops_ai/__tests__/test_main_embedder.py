"""OPS_AI_EMBEDDER wiring in ops_ai/main.py build_app_from_env (B6.2)."""

import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.assistant.__tests__.fake_openai import (
    FakeOpenAiServer,
    ScriptedReply,
    completed_reply,
    fake_server_session,
)
from ops_ai.assistant.openai_llm import DEFAULT_CHAT_MODEL
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.main import build_app_from_env
from ops_ai.search.openai_embedder import OPENAI_DIMENSIONS, OPENAI_EMBEDDING_MODEL
from pydantic import JsonValue
from sqlalchemy.orm import Session, sessionmaker

QUESTION = "declined payments"
CANARY_KEY = "sk-canary-openai-embedder-wiring-71d0"
FAKE_TEXT = "Narrated by the fake OpenAI"
FAKE_MODEL = "fake-served-model-2026"
ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


@pytest.fixture(scope="module")
def pems() -> tuple[str, str]:
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


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pems: tuple[str, str]) -> pytest.MonkeyPatch:
    for name in (
        "OPS_AI_JWT_PUBLIC_KEY_PEM",
        "OPS_AI_EMBEDDER",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_CHAT_MODEL",
        "OPENAI_FALLBACK_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(pems[1])
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    return monkeypatch


def _merchant_token(private_pem: str, merchant_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _seed_declined_payment(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> uuid.UUID:
    now = datetime.now(UTC)
    payment_id = uuid.uuid4()
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=now,
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant_id,
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
        apply_payment_event(session, lambda: now, event)
    return payment_id


@dataclass(frozen=True)
class AskResult:
    status_code: int
    body: dict[str, JsonValue]

    def evidence_refs(self) -> list[str]:
        evidence = self.body["evidence"]
        if not isinstance(evidence, list):
            raise AssertionError(f"evidence is not a list: {evidence!r}")
        refs: list[str] = []
        for item in evidence:
            if not isinstance(item, dict) or not isinstance(item["ref"], str):
                raise AssertionError(f"evidence item without a string ref: {item!r}")
            refs.append(item["ref"])
        return refs


def _ask(pems: tuple[str, str], merchant_id: uuid.UUID) -> AskResult:
    client = TestClient(build_app_from_env())
    headers = {"Authorization": f"Bearer {_merchant_token(pems[0], merchant_id)}"}
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=headers)
    body: JsonValue = response.json()
    if not isinstance(body, dict):
        raise AssertionError(f"response body is not a JSON object: {response.text!r}")
    return AskResult(status_code=response.status_code, body=body)


def _embeddings_reply() -> ScriptedReply:
    vector: list[JsonValue] = [1.0, *(0.0 for _ in range(OPENAI_DIMENSIONS - 1))]
    item: dict[str, JsonValue] = {"object": "embedding", "index": 0, "embedding": vector}
    return ScriptedReply(json_body={"object": "list", "data": [item]})


def test_build_app_from_env_refuses_the_openai_embedder_without_an_api_key(env: pytest.MonkeyPatch) -> None:
    # Arrange
    env.setenv("OPS_AI_EMBEDDER", "openai")

    # Act
    with pytest.raises(RuntimeError) as exc:
        build_app_from_env()

    # Assert
    assert type(exc.value) is RuntimeError


def test_build_app_from_env_refuses_the_openai_embedder_with_an_empty_api_key(
    env: pytest.MonkeyPatch,
) -> None:
    # Arrange
    env.setenv("OPS_AI_EMBEDDER", "openai")
    env.setenv("OPENAI_API_KEY", "")

    # Act
    with pytest.raises(RuntimeError) as exc:
        build_app_from_env()

    # Assert
    assert type(exc.value) is RuntimeError


@pytest.mark.usefixtures("redis_client")
def test_build_app_from_env_sends_the_question_to_the_embeddings_endpoint_with_the_openai_embedder(
    env: pytest.MonkeyPatch,
    serve: ServeFake,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    server = serve([_embeddings_reply(), completed_reply(FAKE_TEXT, FAKE_MODEL)])
    env.setenv("OPS_AI_EMBEDDER", "openai")
    env.setenv("OPENAI_API_KEY", CANARY_KEY)
    env.setenv("OPENAI_BASE_URL", server.base_url)
    merchant_id = uuid.uuid4()
    declined_id = _seed_declined_payment(session_factory, merchant_id)

    # Act
    result = _ask(pems, merchant_id)

    # Assert
    assert result.status_code == 200
    assert (result.body["mode"], result.body["text"], result.body["model"]) == ("llm", FAKE_TEXT, FAKE_MODEL)
    assert f"payment:{declined_id}" in result.evidence_refs()
    assert [request.path for request in server.requests] == ["/v1/embeddings", "/v1/responses"]
    embeddings_request = server.requests[0]
    assert embeddings_request.headers["authorization"] == f"Bearer {CANARY_KEY}"
    assert embeddings_request.json_object() == {
        "model": OPENAI_EMBEDDING_MODEL,
        "input": [QUESTION],
        "dimensions": OPENAI_DIMENSIONS,
    }
    assert server.requests[1].string_field("model") == DEFAULT_CHAT_MODEL


@pytest.mark.usefixtures("redis_client")
def test_build_app_from_env_still_answers_with_keyword_evidence_when_the_embeddings_call_fails(
    env: pytest.MonkeyPatch,
    serve: ServeFake,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    failing_embeddings = ScriptedReply(status=500, json_body={"error": {"message": "boom"}})
    server = serve([failing_embeddings, completed_reply(FAKE_TEXT, FAKE_MODEL)])
    env.setenv("OPS_AI_EMBEDDER", "openai")
    env.setenv("OPENAI_API_KEY", CANARY_KEY)
    env.setenv("OPENAI_BASE_URL", server.base_url)
    merchant_id = uuid.uuid4()
    declined_id = _seed_declined_payment(session_factory, merchant_id)

    # Act
    result = _ask(pems, merchant_id)

    # Assert
    assert result.status_code == 200
    assert (result.body["mode"], result.body["model"]) == ("llm", FAKE_MODEL)
    assert f"payment:{declined_id}" in result.evidence_refs()
    assert [request.path for request in server.requests] == ["/v1/embeddings", "/v1/responses"]


@pytest.mark.usefixtures("redis_client")
def test_build_app_from_env_uses_the_local_embedder_by_default_even_with_an_openai_key(
    env: pytest.MonkeyPatch,
    serve: ServeFake,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    server = serve([completed_reply(FAKE_TEXT, FAKE_MODEL)])
    env.setenv("OPENAI_API_KEY", CANARY_KEY)
    env.setenv("OPENAI_BASE_URL", server.base_url)
    merchant_id = uuid.uuid4()
    declined_id = _seed_declined_payment(session_factory, merchant_id)

    # Act
    result = _ask(pems, merchant_id)

    # Assert
    assert result.status_code == 200
    assert f"payment:{declined_id}" in result.evidence_refs()
    assert [request.path for request in server.requests] == ["/v1/responses"]


@pytest.mark.usefixtures("redis_client")
def test_build_app_from_env_with_the_local_embedder_makes_no_embeddings_request_and_uses_the_mock_llm(
    env: pytest.MonkeyPatch,
    serve: ServeFake,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    server = serve([completed_reply(FAKE_TEXT, FAKE_MODEL)])
    env.setenv("OPS_AI_EMBEDDER", "local")
    env.setenv("OPENAI_BASE_URL", server.base_url)
    merchant_id = uuid.uuid4()
    _seed_declined_payment(session_factory, merchant_id)

    # Act
    result = _ask(pems, merchant_id)

    # Assert
    assert (result.status_code, result.body["mode"], result.body["model"]) == (200, "llm", "mock")
    assert server.requests == []
