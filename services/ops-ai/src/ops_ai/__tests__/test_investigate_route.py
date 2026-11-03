"""Tests for POST /v1/assistant/investigate, written from docs/specs/sq1.md section 8 (API)."""

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
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

QUESTION = "15% of my card payments are failing this afternoon - what is the root cause and how do we fix it?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
ROUTE = "/v1/assistant/investigate"
WINDOW_START = TEST_NOW - timedelta(hours=6)
BASELINE_START = WINDOW_START - timedelta(days=7)
TOP_LEVEL_KEYS = {
    "intent",
    "cause_label",
    "mode",
    "text",
    "model",
    "window",
    "baseline",
    "suggested_fixes",
    "tool_calls",
    "evidence",
}
AGGREGATE_KEYS = {"start", "end", "total", "failed", "rate", "by_bin", "by_code"}


@dataclass
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
    a_failed_ids: tuple[uuid.UUID, ...]
    b_failed_ids: tuple[uuid.UUID, ...]


def _event(
    merchant_id: uuid.UUID, occurred_at: datetime, *, declined: bool, payment_id: uuid.UUID
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=occurred_at,
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="issuer_unavailable" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0119" if declined else "4242",
    )


def _insert_incident(session: Session, merchant_id: uuid.UUID | None, incident_id: str) -> None:
    session.execute(
        text(
            "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
            "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
            "(:incident_id, :merchant_id, 'Issuer outage on BIN 400000', 'Issuer timeouts on BIN 400000', "
            "'Rerouted BIN 400000 to acq_b', 'notes', :started_at, :resolved_at, false)"
        ),
        {
            "incident_id": incident_id,
            "merchant_id": merchant_id,
            "started_at": TEST_NOW - timedelta(days=50),
            "resolved_at": TEST_NOW - timedelta(days=50) + timedelta(hours=3),
        },
    )


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    """Merchant A: window 10 declined on 400000 + 10 succeeded, baseline 20 succeeded, one incident.
    Merchant B: 6 declined on 400000 in the window, one incident of its own.
    """
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    a_failed = tuple(uuid.uuid4() for _ in range(10))
    b_failed = tuple(uuid.uuid4() for _ in range(6))
    events: list[PaymentEventV1] = []
    for index, payment_id in enumerate(a_failed):
        events.append(
            _event(
                merchant_a,
                WINDOW_START + timedelta(minutes=10 * (index + 1)),
                declined=True,
                payment_id=payment_id,
            )
        )
    for index in range(10):
        events.append(
            _event(
                merchant_a,
                WINDOW_START + timedelta(minutes=5 + index),
                declined=False,
                payment_id=uuid.uuid4(),
            )
        )
    for index in range(20):
        events.append(
            _event(
                merchant_a,
                BASELINE_START + timedelta(minutes=index + 1),
                declined=False,
                payment_id=uuid.uuid4(),
            )
        )
    for index, payment_id in enumerate(b_failed):
        events.append(
            _event(
                merchant_b, WINDOW_START + timedelta(minutes=index + 1), declined=True, payment_id=payment_id
            )
        )
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
        _insert_incident(session, merchant_a, "inc_a")
        _insert_incident(session, merchant_b, "inc_b")
    return Seeded(merchant_a, merchant_b, a_failed, b_failed)


def _client(
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime],
    public_key_pem: str,
    llm: LlmPort,
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=llm, jwt_public_key_pem=public_key_pem, clock=clock
    )
    return TestClient(create_app(deps))


def _token(private_pem: str, merchant_id: uuid.UUID) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post(
    client: TestClient, key_pair: KeyPair, merchant_id: uuid.UUID, question: str = QUESTION
) -> dict[str, object]:
    response = client.post(
        ROUTE, json={"question": question}, headers=_bearer(_token(key_pair.private_pem, merchant_id))
    )
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def _refs(body: dict[str, object]) -> list[str]:
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    return [str(item["ref"]) for item in evidence]


# --- success shape ---------------------------------------------------------------------------------------


def test_investigate_returns_exactly_the_specified_top_level_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post(client, key_pair, seeded.merchant_a)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS


def test_investigate_returns_the_cause_label_the_narration_and_the_fixes(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post(client, key_pair, seeded.merchant_a)

    # Assert
    assert (body["intent"], body["cause_label"]) == ("failure_spike", "issuer_outage_on_bin")
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert body["suggested_fixes"] == ["Rerouted BIN 400000 to acq_b"]


def test_investigate_window_and_baseline_have_the_specified_shape_and_numbers(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post(client, key_pair, seeded.merchant_a)

    # Assert
    window = body["window"]
    baseline = body["baseline"]
    assert isinstance(window, dict)
    assert isinstance(baseline, dict)
    assert set(window) == AGGREGATE_KEYS
    assert set(baseline) == AGGREGATE_KEYS
    assert (datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])) == (
        WINDOW_START,
        TEST_NOW,
    )
    assert {k: window[k] for k in ("total", "failed", "rate", "by_bin", "by_code")} == {
        "total": 20,
        "failed": 10,
        "rate": 0.5,
        "by_bin": [{"card_bin": "400000", "failed": 10, "total": 10}],
        "by_code": [{"decline_code": "issuer_unavailable", "count": 10}],
    }
    assert (datetime.fromisoformat(baseline["start"]), datetime.fromisoformat(baseline["end"])) == (
        BASELINE_START,
        WINDOW_START,
    )
    assert {k: baseline[k] for k in ("total", "failed", "rate", "by_bin", "by_code")} == {
        "total": 20,
        "failed": 0,
        "rate": 0.0,
        "by_bin": [],
        "by_code": [],
    }


def test_investigate_lists_the_tool_calls_and_the_evidence_with_their_own_key_sets(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post(client, key_pair, seeded.merchant_a)

    # Assert
    assert body["tool_calls"] == [
        {
            "name": "aggregate_failures",
            "args": {"start": WINDOW_START.isoformat(), "end": TEST_NOW.isoformat()},
        },
        {"name": "compare_baseline", "args": {"window_start": WINDOW_START.isoformat(), "days": "7"}},
        {"name": "search_incidents", "args": {"query": "BIN 400000 issuer"}},
    ]
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert all(set(item) == {"ref", "summary"} for item in evidence)
    newest_five = [f"payment:{pid}" for pid in reversed(seeded.a_failed_ids[5:])]
    assert _refs(body) == [
        "aggregate:window",
        "aggregate:baseline",
        "aggregate:bin:400000",
        "incident:inc_a",
        *newest_five,
    ]


def test_investigate_takes_now_from_the_injected_clock(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    later = TEST_NOW + timedelta(minutes=10)
    client = _client(session_factory, lambda: later, key_pair.public_pem, RecordingLlm())
    token = _token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    window = response.json()["window"]
    assert response.status_code == 200
    assert (datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])) == (
        later - timedelta(hours=6),
        later,
    )


def test_investigate_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_analysis(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    ok_client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    down = DownLlm()
    down_client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, down)
    with_llm = _post(ok_client, key_pair, seeded.merchant_a)

    # Act
    fallback = _post(down_client, key_pair, seeded.merchant_a)

    # Assert
    assert (fallback["mode"], fallback["text"], fallback["model"]) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert {k: v for k, v in fallback.items() if k not in ("mode", "text", "model")} == {
        k: v for k, v in with_llm.items() if k not in ("mode", "text", "model")
    }


def test_investigate_unsupported_question_returns_the_empty_t2_fallback_body(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    body = _post(client, key_pair, seeded.merchant_a, question="What is my settlement balance?")

    # Assert
    assert body == {
        "intent": "unsupported",
        "cause_label": "unsupported",
        "mode": "t2_fallback",
        "text": None,
        "model": None,
        "window": None,
        "baseline": None,
        "suggested_fixes": [],
        "tool_calls": [],
        "evidence": [],
    }
    assert llm.calls == []


def test_investigate_scopes_everything_to_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_token(key_pair.private_pem, seeded.merchant_b))
    )

    # Act
    body = response.json()

    # Assert: merchant B has 6 declines and its own incident only, never merchant A's data
    assert response.status_code == 200
    assert body["window"]["total"] == 6
    assert body["window"]["failed"] == 6
    assert _refs(body)[3] == "incident:inc_b"
    assert "inc_a" not in response.text
    for payment_id in seeded.a_failed_ids:
        assert str(payment_id) not in response.text
        assert all(
            str(payment_id) not in item.ref + item.summary for call in llm.calls for item in call.evidence
        )


# --- auth and validation ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer garbage-token-canary-5521"}, {"Authorization": "Basic Z2FyYmFnZQ=="}],
    ids=["no_authorization_header", "garbage_token", "not_a_bearer_scheme"],
)
def test_investigate_without_a_valid_token_returns_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, headers: dict[str, str]
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert "garbage-token-canary-5521" not in response.text
    assert llm.calls == []


def test_investigate_rejects_a_token_signed_by_another_key_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, other_key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    token = _token(other_key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert llm.calls == []


def test_investigate_rejects_an_expired_token_once_the_injected_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW + timedelta(seconds=901), key_pair.public_pem, RecordingLlm()
    )
    token = _token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


def test_investigate_rejects_a_merchant_id_in_the_body_with_422_and_does_not_call_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    token = _token(key_pair.private_pem, seeded.merchant_a)
    body = {"question": QUESTION, "merchant_id": str(seeded.merchant_b)}

    # Act
    response = client.post(ROUTE, json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 422
    assert str(seeded.b_failed_ids[0]) not in response.text
    assert llm.calls == []


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "x" * 2001}, {"question": 123}, {"question": QUESTION, "limit": 5}],
    ids=["no_question", "empty_question", "question_2001_chars", "question_not_a_string", "extra_field"],
)
def test_investigate_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    token = _token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post(ROUTE, json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 422


# --- /ask unchanged --------------------------------------------------------------------------------------


def test_ask_response_shape_is_unchanged_by_the_investigate_route(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    token = _token(key_pair.private_pem, seeded.merchant_a)

    # Act
    response = client.post(
        "/v1/assistant/ask", json={"question": "declined payments"}, headers=_bearer(token)
    )

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"mode", "text", "model", "evidence"}
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert all(set(item) == {"ref", "summary"} for item in body["evidence"])
    assert body["evidence"][0] == {
        "ref": "aggregate:decline_code:issuer_unavailable",
        "summary": "10 declined payments with decline code issuer_unavailable",
    }
