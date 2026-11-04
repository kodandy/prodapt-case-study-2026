"""OpsAiAppDeps.embedder: /v1/assistant/ask passes it to ask() (B6.2)."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.clock import system_clock
from ops_ai.search.__tests__.embedded_documents import seed_embedded
from ops_ai.search.__tests__.fake_embedders import RecordingEmbedder, UnavailableEmbedder
from ops_ai.search.embedder import LocalHashEmbedder
from sqlalchemy.orm import Session, sessionmaker

TYPO_QUESTION = "insufficent"
TYPO_TITLE = "Card payment declined"
TYPO_BODY = "insufficient funds"


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


def _client(deps: OpsAiAppDeps) -> TestClient:
    return TestClient(create_app(deps))


def _refs(response_json: object) -> list[str]:
    if not isinstance(response_json, dict) or not isinstance(response_json["evidence"], list):
        raise AssertionError(f"unexpected response body: {response_json!r}")
    return [str(item["ref"]) for item in response_json["evidence"]]


def test_ops_ai_app_deps_embedder_defaults_to_the_local_hash_embedder() -> None:
    # Arrange
    deps = OpsAiAppDeps(
        session_factory=sessionmaker[Session](),
        llm=MockLlm(),
        jwt_public_key_pem="unused",
        clock=system_clock,
    )

    # Act
    embedder = deps.embedder

    # Assert
    assert type(embedder) is LocalHashEmbedder


def test_ask_route_finds_a_typo_document_with_the_default_embedder(
    pems: tuple[str, str], session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    typo_doc = seed_embedded(session_factory, merchant_id, TYPO_TITLE, TYPO_BODY)
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=MockLlm(), jwt_public_key_pem=pems[1], clock=system_clock
    )
    headers = {"Authorization": f"Bearer {_merchant_token(pems[0], merchant_id)}"}

    # Act
    response = _client(deps).post("/v1/assistant/ask", json={"question": TYPO_QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 200
    assert _refs(response.json()) == [f"payment:{typo_doc}"]


def test_ask_route_embeds_the_question_with_the_embedder_from_the_deps(
    pems: tuple[str, str], session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    typo_doc = seed_embedded(session_factory, merchant_id, TYPO_TITLE, TYPO_BODY)
    embedder = RecordingEmbedder(LocalHashEmbedder())
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=MockLlm(),
        jwt_public_key_pem=pems[1],
        clock=system_clock,
        embedder=embedder,
    )
    headers = {"Authorization": f"Bearer {_merchant_token(pems[0], merchant_id)}"}

    # Act
    response = _client(deps).post("/v1/assistant/ask", json={"question": TYPO_QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 200
    assert embedder.calls == [[TYPO_QUESTION]]
    assert _refs(response.json()) == [f"payment:{typo_doc}"]


def test_ask_route_answers_200_without_vector_evidence_when_the_embedder_is_unavailable(
    pems: tuple[str, str], session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    seed_embedded(session_factory, merchant_id, TYPO_TITLE, TYPO_BODY)
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=MockLlm(),
        jwt_public_key_pem=pems[1],
        clock=system_clock,
        embedder=UnavailableEmbedder(),
    )
    headers = {"Authorization": f"Bearer {_merchant_token(pems[0], merchant_id)}"}

    # Act
    response = _client(deps).post("/v1/assistant/ask", json={"question": TYPO_QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 200
    assert (response.json()["mode"], _refs(response.json())) == ("llm", [])
