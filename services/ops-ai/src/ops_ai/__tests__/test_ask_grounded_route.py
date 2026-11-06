"""R1-5: POST /v1/assistant/ask?grounded=true adds per-sentence citations; the default body is unchanged."""

import uuid
from datetime import timedelta

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.citations import CANNOT_ANSWER_TEXT
from ops_ai.assistant.llm_port import LlmPort, MockLlm
from ops_ai.ingest.consumer import apply_payment_event
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

QUESTION = "declined payments"
AGGREGATE_REF = "aggregate:decline_code:card_declined"
DEFAULT_KEYS = {"mode", "text", "model", "evidence"}
GROUNDED_KEYS = DEFAULT_KEYS | {"grounded", "citations", "unsupported_sentences", "relevant_refs"}


class Keys:
    def __init__(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        self.public_pem = (
            key.public_key()
            .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
            .decode()
        )


@pytest.fixture(scope="module")
def keys() -> Keys:
    return Keys()


def _client(session_factory: sessionmaker[Session], keys: Keys, llm: LlmPort) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=llm, jwt_public_key_pem=keys.public_pem, clock=lambda: TEST_NOW
    )
    return TestClient(create_app(deps))


def _headers(keys: Keys, merchant: uuid.UUID) -> dict[str, str]:
    issued_at = int(TEST_NOW.timestamp())
    claims = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant}",
        "merchant_id": str(merchant),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, keys.private_pem, algorithm='RS256')}"}


def _seed_declined(session_factory: sessionmaker[Session], merchant: uuid.UUID) -> uuid.UUID:
    payment_id = uuid.uuid4()
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=TEST_NOW - timedelta(minutes=5),
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant,
        amount_minor=1500,
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


@pytest.mark.parametrize("path", ["/v1/assistant/ask", "/v1/assistant/ask?grounded=false"])
def test_ask_without_grounded_keeps_exactly_the_original_body(
    session_factory: sessionmaker[Session], keys: Keys, path: str
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declined(session_factory, merchant)
    client = _client(session_factory, keys, MockLlm())

    # Act
    response = client.post(path, json={"question": QUESTION}, headers=_headers(keys, merchant))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == DEFAULT_KEYS
    assert body["text"].startswith(f"Found 2 evidence items for: {QUESTION}\n")


def test_grounded_ask_returns_cited_sentences_with_verbatim_excerpts(
    session_factory: sessionmaker[Session], keys: Keys
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    payment_id = _seed_declined(session_factory, merchant)
    client = _client(session_factory, keys, MockLlm())

    # Act
    response = client.post(
        "/v1/assistant/ask?grounded=true", json={"question": QUESTION}, headers=_headers(keys, merchant)
    )

    # Assert
    body = response.json()
    summaries = {item["ref"]: item["summary"] for item in body["evidence"]}
    assert response.status_code == 200
    assert set(body) == GROUNDED_KEYS
    assert (body["mode"], body["model"], body["grounded"]) == ("llm", "mock", True)
    assert body["relevant_refs"] == [AGGREGATE_REF, f"payment:{payment_id}"]
    assert [[source["ref"] for source in cited["sources"]] for cited in body["citations"]] == [
        [AGGREGATE_REF],
        [f"payment:{payment_id}"],
    ]
    assert all(
        source["excerpt"] and source["excerpt"] in summaries[source["ref"]]
        for cited in body["citations"]
        for source in cited["sources"]
    )
    assert body["text"] == "\n".join(cited["sentence"] for cited in body["citations"])
    assert body["unsupported_sentences"] == [
        {"sentence": f"Found 2 evidence items for: {QUESTION}", "reason": "no_citation", "refs": []}
    ]


def test_grounded_ask_with_nothing_relevant_says_it_cannot_answer(
    session_factory: sessionmaker[Session], keys: Keys
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declined(session_factory, merchant)
    client = _client(session_factory, keys, MockLlm())

    # Act
    response = client.post(
        "/v1/assistant/ask?grounded=true",
        json={"question": "weather forecast tomorrow"},
        headers=_headers(keys, merchant),
    )

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert (body["mode"], body["text"], body["model"], body["grounded"]) == (
        "llm",
        CANNOT_ANSWER_TEXT,
        None,
        False,
    )
    assert (body["citations"], body["unsupported_sentences"], body["relevant_refs"]) == ([], [], [])


def test_grounded_ask_never_cites_another_merchants_records(
    session_factory: sessionmaker[Session], keys: Keys
) -> None:
    # Arrange
    mine = uuid.uuid4()
    other = uuid.uuid4()
    _seed_declined(session_factory, other)
    client = _client(session_factory, keys, MockLlm())

    # Act
    response = client.post(
        "/v1/assistant/ask?grounded=true", json={"question": QUESTION}, headers=_headers(keys, mine)
    )

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert (body["text"], body["grounded"], body["evidence"], body["citations"]) == (
        CANNOT_ANSWER_TEXT,
        False,
        [],
        [],
    )
