"""HTTP tests for the session and memory routes, written from docs/specs/memory.md (HTTP section)."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.ingest.consumer import apply_payment_event
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
SESSIONS = "/v1/assistant/sessions"
MEMORIES = "/v1/assistant/memories"
MIDNIGHT_TODAY = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
MIDNIGHT_YESTERDAY = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
TODAY_QUESTION = "what is my card failure rate today?"
FOLLOW_UP = "what about yesterday?"
PROPOSAL_QUESTION = "remember that refunds go through acq_b"
PROPOSAL_CONTENT = "refunds go through acq_b"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
SESSION_NOT_FOUND = {"detail": "session not found"}
PROPOSAL_NOT_FOUND = {"detail": "proposal not found"}
MEMORY_NOT_FOUND = {"detail": "memory not found"}
TURN_KEYS = {
    "session_id",
    "turn_id",
    "intent",
    "metric",
    "window",
    "mode",
    "text",
    "model",
    "result",
    "tool_calls",
    "evidence",
    "memories",
    "pending_memory",
}
MEMORY_KEYS = {"memory_id", "content", "source_session_id", "source_turn_id", "supersedes_id", "created_at"}


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


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


@dataclass(frozen=True)
class Seeded:
    merchant_a: uuid.UUID
    merchant_b: uuid.UUID


def _event(merchant_id: uuid.UUID, occurred_at: datetime, *, declined: bool) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=occurred_at,
        entity_version=1,
        payment_id=uuid.uuid4(),
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


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    """Merchant A: today 2 succeeded; yesterday 4 total / 1 declined. Merchant B: yesterday 2 declined."""
    a, b = uuid.uuid4(), uuid.uuid4()
    events = [_event(a, MIDNIGHT_TODAY + timedelta(hours=6, minutes=i), declined=False) for i in range(2)]
    events += [
        _event(a, MIDNIGHT_YESTERDAY + timedelta(hours=10, minutes=i), declined=i == 0) for i in range(4)
    ]
    events += [
        _event(b, MIDNIGHT_YESTERDAY + timedelta(hours=12, minutes=i), declined=True) for i in range(2)
    ]
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
    return Seeded(merchant_a=a, merchant_b=b)


@pytest.fixture
def llm() -> RecordingLlm:
    return RecordingLlm()


@pytest.fixture
def client(
    session_factory: sessionmaker[Session], redis_client: Redis, key_pair: KeyPair, llm: RecordingLlm
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=llm,
        jwt_public_key_pem=key_pair.public_pem,
        clock=lambda: TEST_NOW,
        redis=redis_client,
    )
    return TestClient(create_app(deps))


def _claims(subject: str, extra: dict[str, object]) -> dict[str, object]:
    issued_at = int(TEST_NOW.timestamp())
    return {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }


def _merchant_headers(key_pair: KeyPair, merchant_id: uuid.UUID) -> dict[str, str]:
    extra: dict[str, object] = {"merchant_id": str(merchant_id), "roles": ["merchant_admin"]}
    claims = _claims(f"merchant:{merchant_id}", extra)
    return {"Authorization": f"Bearer {jwt.encode(claims, key_pair.private_pem, algorithm='RS256')}"}


def _user_headers(key_pair: KeyPair) -> dict[str, str]:
    user_id = uuid.uuid4()
    claims = _claims(f"user:{user_id}", {"user_id": str(user_id), "roles": ["consumer"]})
    return {"Authorization": f"Bearer {jwt.encode(claims, key_pair.private_pem, algorithm='RS256')}"}


def _create_session(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post(SESSIONS, headers=headers)
    assert response.status_code == 201
    session_id = response.json()["session_id"]
    assert isinstance(session_id, str)
    return session_id


def _ask(client: TestClient, headers: dict[str, str], session_id: str, question: str) -> dict[str, object]:
    response = client.post(f"{SESSIONS}/{session_id}/turns", json={"question": question}, headers=headers)
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def _pending_proposal_id(turn: dict[str, object]) -> str:
    pending = turn["pending_memory"]
    assert isinstance(pending, dict)
    return str(pending["proposal_id"])


def _confirm(
    client: TestClient, headers: dict[str, str], session_id: str, proposal_id: str
) -> dict[str, str]:
    url = f"{SESSIONS}/{session_id}/memory-proposals/{proposal_id}/confirm"
    response = client.post(url, headers=headers)
    assert response.status_code == 201
    body: dict[str, str] = response.json()
    return body


def _remember(client: TestClient, headers: dict[str, str]) -> dict[str, str]:
    """Proposes and confirms PROPOSAL_CONTENT in a fresh session; returns the stored memory."""
    session_id = _create_session(client, headers)
    turn = _ask(client, headers, session_id, PROPOSAL_QUESTION)
    return _confirm(client, headers, session_id, _pending_proposal_id(turn))


def _list_memories(client: TestClient, headers: dict[str, str]) -> list[dict[str, str]]:
    response = client.get(MEMORIES, headers=headers)
    assert response.status_code == 200
    memories: list[dict[str, str]] = response.json()["memories"]
    return memories


def _window(body: dict[str, object]) -> tuple[datetime, datetime]:
    window = body["window"]
    assert isinstance(window, dict)
    return datetime.fromisoformat(window["start"]), datetime.fromisoformat(window["end"])


# --- sessions --------------------------------------------------------------------------------------------


def test_create_session_returns_201_with_the_id_and_the_ttl(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)

    # Act
    response = client.post(SESSIONS, headers=headers)

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"session_id", "expires_in_seconds"}
    assert str(uuid.UUID(body["session_id"])) == body["session_id"]
    assert body["expires_in_seconds"] == 1800
    assert response.headers["Cache-Control"] == "no-store"


def test_get_a_fresh_session_returns_empty_state_and_no_turns(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)

    # Act
    response = client.get(f"{SESSIONS}/{session_id}", headers=headers)

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "session_id": session_id,
        "state": {"metric": None, "window": None},
        "turns": [],
    }
    assert response.headers["Cache-Control"] == "no-store"


def test_get_session_lists_the_turns_and_the_state_after_two_questions(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)
    first = _ask(client, headers, session_id, TODAY_QUESTION)
    second = _ask(client, headers, session_id, FOLLOW_UP)

    # Act
    response = client.get(f"{SESSIONS}/{session_id}", headers=headers)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert [turn["turn_id"] for turn in body["turns"]] == [first["turn_id"], second["turn_id"]]
    assert all(set(turn) == {"turn_id", "question", "intent", "metric", "window"} for turn in body["turns"])
    assert [(turn["question"], turn["intent"], turn["metric"]) for turn in body["turns"]] == [
        (TODAY_QUESTION, "metric_query", "failure_rate"),
        (FOLLOW_UP, "metric_query", "failure_rate"),
    ]
    assert body["state"]["metric"] == "failure_rate"
    assert _window(body["state"]) == (MIDNIGHT_YESTERDAY, MIDNIGHT_TODAY)


def test_a_session_that_expired_in_redis_is_a_404(
    client: TestClient, redis_client: Redis, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)
    redis_client.flushdb()

    # Act
    response = client.get(f"{SESSIONS}/{session_id}", headers=headers)

    # Assert
    assert response.status_code == 404
    assert response.json() == SESSION_NOT_FOUND


# --- turns -----------------------------------------------------------------------------------------------


def test_turn_response_has_exactly_the_specified_keys_and_values(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)

    # Act
    request_body = {"question": TODAY_QUESTION}
    response = client.post(f"{SESSIONS}/{session_id}/turns", json=request_body, headers=headers)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == TURN_KEYS
    assert body["session_id"] == session_id
    assert (body["intent"], body["metric"]) == ("metric_query", "failure_rate")
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert _window(body) == (MIDNIGHT_TODAY, TEST_NOW)
    assert body["result"] == {"total": 2, "failed": 0, "rate": 0.0}
    assert body["tool_calls"] == [
        {
            "name": "aggregate_failures",
            "args": {
                "start": MIDNIGHT_TODAY.isoformat(),
                "end": TEST_NOW.isoformat(),
                "metric": "failure_rate",
            },
        }
    ]
    assert [item["ref"] for item in body["evidence"]] == ["aggregate:window"]
    assert all(set(item) == {"ref", "summary"} for item in body["evidence"])
    assert (body["memories"], body["pending_memory"]) == ([], None)
    assert response.headers["Cache-Control"] == "no-store"


def test_follow_up_turn_reuses_the_metric_and_returns_yesterdays_numbers_for_the_merchant(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)
    _ask(client, headers, session_id, TODAY_QUESTION)

    # Act
    body = _ask(client, headers, session_id, FOLLOW_UP)

    # Assert: merchant B's 2 declines yesterday are not counted
    assert (body["intent"], body["metric"]) == ("metric_query", "failure_rate")
    assert _window(body) == (MIDNIGHT_YESTERDAY, MIDNIGHT_TODAY)
    assert body["result"] == {"total": 4, "failed": 1, "rate": 0.25}


def test_unsupported_turn_returns_a_t2_fallback_without_result_or_window(
    client: TestClient, key_pair: KeyPair, seeded: Seeded, llm: RecordingLlm
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)

    # Act
    body = _ask(client, headers, session_id, "What is my settlement balance?")

    # Assert
    assert (body["intent"], body["metric"], body["window"], body["result"]) == (
        "unsupported",
        None,
        None,
        None,
    )
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    assert body["tool_calls"] == []
    assert llm.calls == []


def test_memory_proposal_turn_returns_the_pending_proposal_and_stores_no_memory(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)

    # Act
    body = _ask(client, headers, session_id, PROPOSAL_QUESTION)

    # Assert
    pending = body["pending_memory"]
    assert isinstance(pending, dict)
    assert set(pending) == {"proposal_id", "content"}
    assert pending["content"] == PROPOSAL_CONTENT
    assert body["intent"] == "memory_proposal"
    assert body["result"] is None
    assert _list_memories(client, headers) == []


# --- confirm and memories --------------------------------------------------------------------------------


def test_confirm_returns_201_with_the_memory_and_its_source_ids(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)
    turn = _ask(client, headers, session_id, PROPOSAL_QUESTION)

    # Act
    response = client.post(
        f"{SESSIONS}/{session_id}/memory-proposals/{_pending_proposal_id(turn)}/confirm", headers=headers
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert set(body) == MEMORY_KEYS
    assert (body["content"], body["source_session_id"], body["source_turn_id"], body["supersedes_id"]) == (
        PROPOSAL_CONTENT,
        session_id,
        turn["turn_id"],
        None,
    )
    assert datetime.fromisoformat(body["created_at"]) == TEST_NOW
    assert response.headers["Cache-Control"] == "no-store"


def test_confirmed_memory_is_listed_and_recalled_in_a_new_session(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    remembered = _remember(client, headers)
    new_session_id = _create_session(client, headers)

    # Act
    listed = _list_memories(client, headers)
    turn = _ask(client, headers, new_session_id, TODAY_QUESTION)

    # Assert
    assert listed == [remembered]
    assert turn["memories"] == [
        {
            "memory_id": remembered["memory_id"],
            "content": PROPOSAL_CONTENT,
            "source_turn_id": remembered["source_turn_id"],
        }
    ]
    evidence = turn["evidence"]
    assert isinstance(evidence, list)
    assert [item["ref"] for item in evidence] == ["aggregate:window", f"memory:{remembered['memory_id']}"]


def test_confirming_the_same_proposal_twice_returns_404_the_second_time(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)
    proposal_id = _pending_proposal_id(_ask(client, headers, session_id, PROPOSAL_QUESTION))
    _confirm(client, headers, session_id, proposal_id)

    # Act
    response = client.post(f"{SESSIONS}/{session_id}/memory-proposals/{proposal_id}/confirm", headers=headers)

    # Assert
    assert response.status_code == 404
    assert response.json() == PROPOSAL_NOT_FOUND
    assert len(_list_memories(client, headers)) == 1


def test_confirming_an_unknown_proposal_returns_404_proposal_not_found(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)

    # Act
    url = f"{SESSIONS}/{session_id}/memory-proposals/{uuid.uuid4()}/confirm"
    response = client.post(url, headers=headers)

    # Assert
    assert response.status_code == 404
    assert response.json() == PROPOSAL_NOT_FOUND


def test_patch_corrects_a_memory_into_a_new_row_that_replaces_it_in_the_list(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    original = _remember(client, headers)

    # Act
    url = f"{MEMORIES}/{original['memory_id']}"
    response = client.patch(url, json={"content": "corrected"}, headers=headers)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == MEMORY_KEYS
    assert body["memory_id"] != original["memory_id"]
    assert (body["content"], body["supersedes_id"]) == ("corrected", original["memory_id"])
    assert (body["source_session_id"], body["source_turn_id"]) == (
        original["source_session_id"],
        original["source_turn_id"],
    )
    assert _list_memories(client, headers) == [body]
    assert response.headers["Cache-Control"] == "no-store"


def test_delete_returns_204_and_the_memory_is_gone_from_the_list(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    original = _remember(client, headers)

    # Act
    response = client.delete(f"{MEMORIES}/{original['memory_id']}", headers=headers)

    # Assert
    assert response.status_code == 204
    assert response.content == b""
    assert _list_memories(client, headers) == []


def test_deleting_the_same_memory_twice_returns_404_memory_not_found(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    original = _remember(client, headers)
    client.delete(f"{MEMORIES}/{original['memory_id']}", headers=headers)

    # Act
    response = client.delete(f"{MEMORIES}/{original['memory_id']}", headers=headers)

    # Assert
    assert response.status_code == 404
    assert response.json() == MEMORY_NOT_FOUND


def test_patch_and_delete_of_an_unknown_memory_return_404_memory_not_found(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    unknown = f"{MEMORIES}/{uuid.uuid4()}"

    # Act
    patched = client.patch(unknown, json={"content": "x"}, headers=headers)
    deleted = client.delete(unknown, headers=headers)

    # Assert
    assert (patched.status_code, patched.json()) == (404, MEMORY_NOT_FOUND)
    assert (deleted.status_code, deleted.json()) == (404, MEMORY_NOT_FOUND)


def test_the_memory_list_contains_only_the_token_merchants_memories(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers_a = _merchant_headers(key_pair, seeded.merchant_a)
    headers_b = _merchant_headers(key_pair, seeded.merchant_b)
    memory_of_a = _remember(client, headers_a)

    # Act
    listed_b = _list_memories(client, headers_b)

    # Assert
    assert listed_b == []
    assert _list_memories(client, headers_a) == [memory_of_a]


# --- cross-merchant isolation ----------------------------------------------------------------------------


def test_another_merchant_gets_the_identical_404_as_for_an_unknown_session(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers_a = _merchant_headers(key_pair, seeded.merchant_a)
    headers_b = _merchant_headers(key_pair, seeded.merchant_b)
    a_session = _create_session(client, headers_a)
    unknown = str(uuid.uuid4())
    body = {"question": TODAY_QUESTION}

    # Act
    foreign = [
        client.get(f"{SESSIONS}/{a_session}", headers=headers_b),
        client.post(f"{SESSIONS}/{a_session}/turns", json=body, headers=headers_b),
        client.post(f"{SESSIONS}/{a_session}/memory-proposals/{uuid.uuid4()}/confirm", headers=headers_b),
    ]
    missing = [
        client.get(f"{SESSIONS}/{unknown}", headers=headers_b),
        client.post(f"{SESSIONS}/{unknown}/turns", json=body, headers=headers_b),
        client.post(f"{SESSIONS}/{unknown}/memory-proposals/{uuid.uuid4()}/confirm", headers=headers_b),
    ]

    # Assert
    assert [(r.status_code, r.json()) for r in foreign] == [(404, SESSION_NOT_FOUND)] * 3
    assert [(r.status_code, r.json()) for r in missing] == [(404, SESSION_NOT_FOUND)] * 3
    assert [r.content for r in foreign] == [r.content for r in missing]


def test_another_merchants_attempts_leave_the_session_and_its_pending_proposal_intact(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers_a = _merchant_headers(key_pair, seeded.merchant_a)
    headers_b = _merchant_headers(key_pair, seeded.merchant_b)
    session_id = _create_session(client, headers_a)
    proposal_id = _pending_proposal_id(_ask(client, headers_a, session_id, PROPOSAL_QUESTION))

    # Act
    stolen = client.post(f"{SESSIONS}/{session_id}/memory-proposals/{proposal_id}/confirm", headers=headers_b)
    turn_body = {"question": TODAY_QUESTION}
    injected = client.post(f"{SESSIONS}/{session_id}/turns", json=turn_body, headers=headers_b)

    # Assert
    assert (stolen.status_code, injected.status_code) == (404, 404)
    assert _list_memories(client, headers_b) == []
    assert _list_memories(client, headers_a) == []
    confirmed = _confirm(client, headers_a, session_id, proposal_id)
    assert confirmed["content"] == PROPOSAL_CONTENT
    turns = client.get(f"{SESSIONS}/{session_id}", headers=headers_a).json()["turns"]
    assert [turn["question"] for turn in turns] == [PROPOSAL_QUESTION]


def test_another_merchant_cannot_patch_or_delete_a_memory_and_the_row_stays_active(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers_a = _merchant_headers(key_pair, seeded.merchant_a)
    headers_b = _merchant_headers(key_pair, seeded.merchant_b)
    memory_of_a = _remember(client, headers_a)
    url = f"{MEMORIES}/{memory_of_a['memory_id']}"

    # Act
    patched = client.patch(url, json={"content": "hijacked"}, headers=headers_b)
    deleted = client.delete(url, headers=headers_b)

    # Assert
    assert (patched.status_code, patched.json()) == (404, MEMORY_NOT_FOUND)
    assert (deleted.status_code, deleted.json()) == (404, MEMORY_NOT_FOUND)
    assert _list_memories(client, headers_a) == [memory_of_a]
    assert _list_memories(client, headers_b) == []


def test_error_responses_are_not_cacheable(client: TestClient, key_pair: KeyPair, seeded: Seeded) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)

    # Act
    response = client.get(f"{SESSIONS}/{uuid.uuid4()}", headers=headers)

    # Assert
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"


# --- validation ------------------------------------------------------------------------------------------


def test_turn_body_with_a_merchant_id_is_a_422_and_runs_nothing(
    client: TestClient, key_pair: KeyPair, seeded: Seeded, llm: RecordingLlm
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)
    body = {"question": TODAY_QUESTION, "merchant_id": str(seeded.merchant_b)}

    # Act
    response = client.post(f"{SESSIONS}/{session_id}/turns", json=body, headers=headers)

    # Assert
    assert response.status_code == 422
    assert llm.calls == []
    assert client.get(f"{SESSIONS}/{session_id}", headers=headers).json()["turns"] == []


def test_patch_body_with_a_merchant_id_is_a_422_and_leaves_the_memory_unchanged(
    client: TestClient, key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    memory = _remember(client, headers)
    body = {"content": "changed", "merchant_id": str(seeded.merchant_b)}

    # Act
    response = client.patch(f"{MEMORIES}/{memory['memory_id']}", json=body, headers=headers)

    # Assert
    assert response.status_code == 422
    assert _list_memories(client, headers) == [memory]


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "x" * 2001}, {"question": 123}, {"question": "ok", "limit": 5}],
    ids=["no_question", "empty_question", "question_2001_chars", "question_not_a_string", "extra_field"],
)
def test_turn_rejects_an_invalid_body_with_422(
    client: TestClient, key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    session_id = _create_session(client, headers)

    # Act
    response = client.post(f"{SESSIONS}/{session_id}/turns", json=body, headers=headers)

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize(
    "body",
    [{}, {"content": ""}, {"content": "x" * 501}, {"content": 123}],
    ids=["no_content", "empty_content", "content_501_chars", "content_not_a_string"],
)
def test_patch_rejects_an_invalid_body_with_422_and_leaves_the_memory_unchanged(
    client: TestClient, key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    headers = _merchant_headers(key_pair, seeded.merchant_a)
    memory = _remember(client, headers)

    # Act
    response = client.patch(f"{MEMORIES}/{memory['memory_id']}", json=body, headers=headers)

    # Assert
    assert response.status_code == 422
    assert _list_memories(client, headers) == [memory]


# --- auth ------------------------------------------------------------------------------------------------

ROUTES = [
    ("POST", SESSIONS, None),
    ("GET", f"{SESSIONS}/{uuid.uuid4()}", None),
    ("POST", f"{SESSIONS}/{uuid.uuid4()}/turns", {"question": TODAY_QUESTION}),
    ("POST", f"{SESSIONS}/{uuid.uuid4()}/memory-proposals/{uuid.uuid4()}/confirm", None),
    ("GET", MEMORIES, None),
    ("PATCH", f"{MEMORIES}/{uuid.uuid4()}", {"content": "x"}),
    ("DELETE", f"{MEMORIES}/{uuid.uuid4()}", None),
]
ROUTE_IDS = ["create", "get_session", "turns", "confirm", "list_memories", "patch_memory", "delete_memory"]


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_without_a_token_return_401_invalid_token(
    client: TestClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    # Arrange: no Authorization header

    # Act
    response = client.request(method, path, json=body)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_with_a_garbage_token_return_401_invalid_token(
    client: TestClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    # Arrange
    headers = {"Authorization": "Bearer garbage-token-canary-5521"}

    # Act
    response = client.request(method, path, json=body, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert "garbage-token-canary-5521" not in response.text


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_with_a_user_token_return_403_forbidden(
    client: TestClient, key_pair: KeyPair, method: str, path: str, body: dict[str, str] | None
) -> None:
    # Arrange
    headers = _user_headers(key_pair)

    # Act
    response = client.request(method, path, json=body, headers=headers)

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
