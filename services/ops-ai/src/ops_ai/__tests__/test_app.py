"""Tests for ops_ai/app.py, written from docs/specs/runtime.md (R5 HTTP routes)."""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.ingest.consumer import apply_payment_event
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

QUESTION = "declined payments"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
AGGREGATE_REF = "aggregate:decline_code:card_declined"
_MISSING = object()


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


def _generate_key_pair() -> KeyPair:
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


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    return _generate_key_pair()


@pytest.fixture(scope="module")
def other_key_pair() -> KeyPair:
    return _generate_key_pair()


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


@dataclass(frozen=True)
class Seeded:
    merchant_a: uuid.UUID
    merchant_b: uuid.UUID
    a_declined: uuid.UUID
    a_succeeded: uuid.UUID
    b_declined: tuple[uuid.UUID, uuid.UUID]


def _ready() -> list[str]:
    return []


def _event(merchant_id: uuid.UUID, payment_id: uuid.UUID, *, declined: bool) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=TEST_NOW - timedelta(minutes=5),
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0002" if declined else "4242",
    )


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    data = Seeded(
        merchant_a=uuid.uuid4(),
        merchant_b=uuid.uuid4(),
        a_declined=uuid.uuid4(),
        a_succeeded=uuid.uuid4(),
        b_declined=(uuid.uuid4(), uuid.uuid4()),
    )
    events = [
        _event(data.merchant_a, data.a_declined, declined=True),
        _event(data.merchant_a, data.a_succeeded, declined=False),
        _event(data.merchant_b, data.b_declined[0], declined=True),
        _event(data.merchant_b, data.b_declined[1], declined=True),
    ]
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
    return data


def _client(
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime],
    public_key_pem: str,
    llm: LlmPort,
    readiness: Callable[[], list[str]] = _ready,
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=llm,
        jwt_public_key_pem=public_key_pem,
        clock=clock,
        readiness=readiness,
    )
    return TestClient(create_app(deps))


def _claims(merchant: uuid.UUID, /, **overrides: object) -> dict[str, object]:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant}",
        "merchant_id": str(merchant),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not _MISSING}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _merchant_token(private_pem: str, merchant_id: uuid.UUID) -> str:
    return jwt.encode(_claims(merchant_id), private_pem, algorithm="RS256")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json_b64url(value: dict[str, object]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


def _alg_none_token(merchant_id: uuid.UUID) -> str:
    return f"{_json_b64url({'alg': 'none', 'typ': 'JWT'})}.{_json_b64url(_claims(merchant_id))}."


def _hs256_confusion_token(merchant_id: uuid.UUID, public_pem: str) -> str:
    signing_input = f"{_json_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_json_b64url(_claims(merchant_id))}"
    signature = hmac.new(public_pem.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _refs(body: dict[str, object]) -> list[str]:
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    return [str(item["ref"]) for item in evidence]


def test_healthz_returns_200(session_factory: sessionmaker[Session], key_pair: KeyPair) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200


def test_readyz_returns_200_ready_with_the_default_readiness(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize(
    "failed",
    [["database"], ["redis"], ["database", "redis"]],
    ids=["database", "redis", "both"],
)
def test_readyz_returns_503_not_ready_listing_the_failed_components(
    session_factory: sessionmaker[Session], key_pair: KeyPair, failed: list[str]
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm(), readiness=lambda: list(failed)
    )

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": failed}


def test_readyz_evaluates_the_readiness_callable_on_every_request(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    outcomes = iter([["database"], []])
    client = _client(
        session_factory,
        lambda: TEST_NOW,
        key_pair.public_pem,
        RecordingLlm(),
        readiness=lambda: next(outcomes),
    )

    # Act
    first = client.get("/readyz")
    second = client.get("/readyz")

    # Assert
    assert (first.status_code, first.json()) == (503, {"status": "not_ready", "failed": ["database"]})
    assert (second.status_code, second.json()) == (200, {"status": "ready"})


def test_healthz_stays_200_while_readiness_reports_failures(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(
        session_factory,
        lambda: TEST_NOW,
        key_pair.public_pem,
        RecordingLlm(),
        readiness=lambda: ["database", "redis"],
    )

    # Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200


def test_ask_returns_the_llm_answer_with_evidence_for_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    token = _merchant_token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"mode", "text", "model", "evidence"}
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert _refs(body) == [AGGREGATE_REF, f"payment:{seeded.a_declined}"]
    assert body["evidence"][0] == {
        "ref": AGGREGATE_REF,
        "summary": "1 declined payments with decline code card_declined",
    }
    assert all(set(item) == {"ref", "summary"} for item in body["evidence"])
    assert [(call.question, [item.ref for item in call.evidence]) for call in llm.calls] == [
        (QUESTION, [AGGREGATE_REF, f"payment:{seeded.a_declined}"])
    ]


def test_ask_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_evidence(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    down = DownLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, down)
    token = _merchant_token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    assert _refs(body) == [AGGREGATE_REF, f"payment:{seeded.a_declined}"]
    assert len(down.calls) == 1


@pytest.mark.parametrize("viewer", ["a", "b"], ids=["merchant_a", "merchant_b"])
def test_ask_never_returns_evidence_that_references_the_other_merchants_payments(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, viewer: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    own_merchant = seeded.merchant_a if viewer == "a" else seeded.merchant_b
    own_payments = [seeded.a_declined] if viewer == "a" else list(seeded.b_declined)
    other_payments = list(seeded.b_declined) if viewer == "a" else [seeded.a_declined, seeded.a_succeeded]
    token = _merchant_token(key_pair.private_pem, own_merchant)

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert sorted(_refs(body)) == sorted([AGGREGATE_REF, *(f"payment:{pid}" for pid in own_payments)])
    for other in other_payments:
        assert str(other) not in response.text
        assert all(str(other) not in item.ref + item.summary for call in llm.calls for item in call.evidence)
    assert (
        body["evidence"][0]["summary"]
        == f"{len(own_payments)} declined payments with decline code card_declined"
    )


def test_ask_rejects_a_merchant_id_in_the_body_with_422_and_does_not_call_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    token = _merchant_token(key_pair.private_pem, seeded.merchant_a)
    body = {"question": QUESTION, "merchant_id": str(seeded.merchant_b)}

    # Act
    response = client.post("/v1/assistant/ask", json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 422
    assert str(seeded.b_declined[0]) not in response.text
    assert llm.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "x" * 2001},
        {"question": 123},
        {"question": QUESTION, "limit": 5},
        ["declined payments"],
    ],
    ids=[
        "no_question",
        "empty_question",
        "question_2001_chars",
        "question_not_a_string",
        "extra_field",
        "list_body",
    ],
)
def test_ask_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    token = _merchant_token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post("/v1/assistant/ask", json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize("length", [1, 2000], ids=["one_char", "max_2000_chars"])
def test_ask_accepts_questions_at_the_length_bounds(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, length: int
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    token = _merchant_token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post("/v1/assistant/ask", json={"question": "q" * length}, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert response.json()["mode"] == "llm"
    assert len(llm.calls) == 1


def _build_headers_no_authorization(_: uuid.UUID, __: KeyPair, ___: KeyPair) -> dict[str, str]:
    return {}


def _build_headers_garbage(_: uuid.UUID, __: KeyPair, ___: KeyPair) -> dict[str, str]:
    return _bearer("garbage-token-canary-5521")


def _build_headers_not_bearer(_: uuid.UUID, __: KeyPair, ___: KeyPair) -> dict[str, str]:
    return {"Authorization": "Basic Z2FyYmFnZS1jYW5hcnk="}


def _build_headers_expired(merchant: uuid.UUID, pair: KeyPair, ___: KeyPair) -> dict[str, str]:
    issued_at = int(TEST_NOW.timestamp())
    claims = _claims(merchant, iat=issued_at - 900, exp=issued_at - 1)
    return _bearer(jwt.encode(claims, pair.private_pem, algorithm="RS256"))


def _build_headers_other_key(merchant: uuid.UUID, _: KeyPair, other: KeyPair) -> dict[str, str]:
    return _bearer(_merchant_token(other.private_pem, merchant))


def _build_headers_alg_none(merchant: uuid.UUID, _: KeyPair, ___: KeyPair) -> dict[str, str]:
    return _bearer(_alg_none_token(merchant))


def _build_headers_hs256_confusion(merchant: uuid.UUID, pair: KeyPair, ___: KeyPair) -> dict[str, str]:
    return _bearer(_hs256_confusion_token(merchant, pair.public_pem))


def _build_headers_missing_merchant_id(merchant: uuid.UUID, pair: KeyPair, ___: KeyPair) -> dict[str, str]:
    claims = _claims(merchant, merchant_id=_MISSING)
    return _bearer(jwt.encode(claims, pair.private_pem, algorithm="RS256"))


REJECTED_AUTH_BUILDERS: dict[str, Callable[[uuid.UUID, KeyPair, KeyPair], dict[str, str]]] = {
    "no_authorization_header": _build_headers_no_authorization,
    "garbage_token": _build_headers_garbage,
    "not_a_bearer_scheme": _build_headers_not_bearer,
    "expired_token": _build_headers_expired,
    "signed_by_another_key_pair": _build_headers_other_key,
    "alg_none": _build_headers_alg_none,
    "hs256_with_public_key_as_secret": _build_headers_hs256_confusion,
    "missing_merchant_id": _build_headers_missing_merchant_id,
}


@pytest.mark.parametrize("case", list(REJECTED_AUTH_BUILDERS), ids=list(REJECTED_AUTH_BUILDERS))
def test_ask_without_a_valid_token_returns_401_invalid_token_and_does_not_echo_it(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    other_key_pair: KeyPair,
    seeded: Seeded,
    case: str,
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    headers = REJECTED_AUTH_BUILDERS[case](seeded.merchant_a, key_pair, other_key_pair)

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    credential = headers.get("Authorization", "").removeprefix("Bearer ").removeprefix("Basic ")
    assert [secret for secret in [credential] if secret and secret in response.text] == []
    assert llm.calls == []


def test_ask_rejects_a_token_once_the_injected_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    now = [TEST_NOW]
    client = _client(session_factory, lambda: now[0], key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem, seeded.merchant_a))
    before_expiry = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=headers)
    now[0] = TEST_NOW + timedelta(seconds=901)

    # Act
    after_expiry = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=headers)

    # Assert
    assert before_expiry.status_code == 200
    assert after_expiry.status_code == 401
    assert after_expiry.json() == INVALID_TOKEN_BODY


def test_ask_with_a_token_for_a_merchant_without_payments_returns_200_with_no_evidence(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    token = _merchant_token(key_pair.private_pem, uuid.uuid4())

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert body["evidence"] == []
    assert str(seeded.a_declined) not in response.text
