"""Tests for POST /v1/assistant/investigate-fraud, written from docs/specs/sq3.md section 6."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ROUTE = "/v1/assistant/investigate-fraud"
QUESTION = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
THIRD_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c03")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
BURST_START = TEST_NOW - timedelta(hours=2)
FIXES = [
    "Add a velocity limit on card payments of 1.00 or less, per card and per client IP.",
    "Require 3-D Secure for card payments of 1.00 or less until the burst stops.",
    "Do not refund or contact the cards in the burst from this channel; the fraud team works the ticket.",
]
TOP_LEVEL_KEYS = {
    "intent",
    "cause_label",
    "mode",
    "text",
    "model",
    "analysis",
    "alerts",
    "suggested_fixes",
    "tool_calls",
    "evidence",
}
ANALYSIS_KEYS = {"start", "end", "micro_payments", "episodes"}
EPISODE_KEYS = {
    "first_payment_id",
    "start",
    "end",
    "payment_ids",
    "micro_payments",
    "card_payments",
    "micro_share",
    "distinct_cards",
    "declined",
    "decline_ratio",
    "card_bins",
    "currencies",
    "flagged",
    "reasons",
}
ALERT_KEYS = {"alert_id", "ticket_id", "queue", "first_payment_id"}

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, 50, 'USD', 'card', :state, 'acq_a', NULL, '400000', :card_ref, 1, "
    ":occurred_at)"
)
INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :window_key, :merchant_id, "
    "'card_testing', :first_payment_id, :started_at, :last_seen_at, 12, '{}'::jsonb, :created_at)"
)
INSERT_TICKET = text(
    "INSERT INTO ops_ai.tickets (ticket_id, source_key, queue, priority, merchant_id, title, reason, "
    "alert_id, created_at) VALUES (:ticket_id, :source_key, :queue, 'high', :merchant_id, "
    "'Card testing suspected', '12 micro payments', :alert_id, :created_at)"
)


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
    own_ids: list[uuid.UUID]
    alert_id: uuid.UUID
    ticket_id: uuid.UUID
    others_ids: list[uuid.UUID]
    others_alert_id: uuid.UUID
    others_ticket_id: uuid.UUID


def _insert_burst(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, first_n: int, start: datetime
) -> list[uuid.UUID]:
    """12 micro card payments 10 s apart on 4 cards, the first 6 declined."""
    ids = [uuid.UUID(int=first_n + index) for index in range(12)]
    with session_factory.begin() as session:
        for index, payment_id in enumerate(ids):
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "state": "declined" if index < 6 else "succeeded",
                    "card_ref": f"{first_n * 10 + index % 4 + 1:032x}",
                    "occurred_at": start + timedelta(seconds=index * 10),
                },
            )
    return ids


def _insert_alert(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    first_payment_id: uuid.UUID,
    started_at: datetime,
) -> tuple[uuid.UUID, uuid.UUID]:
    alert_id, ticket_id = uuid.uuid4(), uuid.uuid4()
    window_key = f"card_testing:{merchant_id}:{first_payment_id}"
    with session_factory.begin() as session:
        session.execute(
            INSERT_ALERT,
            {
                "alert_id": alert_id,
                "window_key": window_key,
                "merchant_id": merchant_id,
                "first_payment_id": first_payment_id,
                "started_at": started_at,
                "last_seen_at": started_at + timedelta(seconds=110),
                "created_at": started_at,
            },
        )
        session.execute(
            INSERT_TICKET,
            {
                "ticket_id": ticket_id,
                "source_key": window_key,
                "queue": "fraud",
                "merchant_id": merchant_id,
                "alert_id": alert_id,
                "created_at": started_at,
            },
        )
    return alert_id, ticket_id


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    """The merchant has a flagged burst with an alert and a ticket; another merchant has its own."""
    own_ids = _insert_burst(session_factory, MERCHANT, 1, BURST_START)
    alert_id, ticket_id = _insert_alert(session_factory, MERCHANT, own_ids[0], BURST_START)
    others_ids = _insert_burst(session_factory, OTHER_MERCHANT, 1000, BURST_START)
    others_alert_id, others_ticket_id = _insert_alert(
        session_factory, OTHER_MERCHANT, others_ids[0], BURST_START
    )
    return Seeded(own_ids, alert_id, ticket_id, others_ids, others_alert_id, others_ticket_id)


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


def _claims(subject: str, extra: dict[str, object], roles: list[str]) -> dict[str, object]:
    issued_at = int(TEST_NOW.timestamp())
    return {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }


def _merchant_token(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _user_token(private_pem: str) -> str:
    claims = _claims(f"user:{USER}", {"user_id": str(USER)}, ["consumer"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post_ok(
    client: TestClient,
    key_pair: KeyPair,
    merchant_id: uuid.UUID = MERCHANT,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    response = client.post(
        ROUTE,
        json=body if body is not None else {"question": QUESTION},
        headers=_bearer(_merchant_token(key_pair.private_pem, merchant_id)),
    )
    assert response.status_code == 200
    parsed: dict[str, object] = response.json()
    return parsed


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _episode(body: dict[str, object]) -> dict[str, object]:
    episodes = _as_list(_as_dict(body["analysis"])["episodes"])
    assert len(episodes) == 1
    return _as_dict(episodes[0])


# --- success shape ----------------------------------------------------------------------------------------


def test_investigate_fraud_returns_exactly_the_specified_keys_at_every_level(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS
    assert set(_as_dict(body["analysis"])) == ANALYSIS_KEYS
    assert set(_episode(body)) == EPISODE_KEYS
    (alert,) = _as_list(body["alerts"])
    assert set(_as_dict(alert)) == ALERT_KEYS
    (tool_call,) = _as_list(body["tool_calls"])
    assert set(_as_dict(tool_call)) == {"name", "args"}
    assert {frozenset(_as_dict(item)) for item in _as_list(body["evidence"])} == {
        frozenset({"ref", "summary"})
    }


def test_investigate_fraud_returns_the_label_the_narration_and_the_playbook(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert (body["intent"], body["cause_label"]) == ("card_testing", "card_testing")
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert body["suggested_fixes"] == FIXES


def test_investigate_fraud_returns_the_flagged_episode_with_its_features(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    episode = _episode(body)
    assert datetime.fromisoformat(str(episode["start"])) == BURST_START
    assert datetime.fromisoformat(str(episode["end"])) == BURST_START + timedelta(seconds=110)
    assert {k: v for k, v in episode.items() if k not in ("start", "end")} == {
        "first_payment_id": str(seeded.own_ids[0]),
        "payment_ids": [str(payment_id) for payment_id in seeded.own_ids],
        "micro_payments": 12,
        "card_payments": 12,
        "micro_share": 1.0,
        "distinct_cards": 4,
        "declined": 6,
        "decline_ratio": 0.5,
        "card_bins": ["400000"],
        "currencies": ["USD"],
        "flagged": True,
        "reasons": ["micro_burst", "many_cards", "high_decline_ratio"],
    }


def test_investigate_fraud_returns_the_alert_with_its_ticket_and_queue(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert body["alerts"] == [
        {
            "alert_id": str(seeded.alert_id),
            "ticket_id": str(seeded.ticket_id),
            "queue": "fraud",
            "first_payment_id": str(seeded.own_ids[0]),
        }
    ]


def test_investigate_fraud_lists_the_analyze_fraud_call_over_the_24_hours_before_the_injected_clock(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert body["tool_calls"] == [
        {
            "name": "analyze_fraud",
            "args": {"start": (TEST_NOW - timedelta(hours=24)).isoformat(), "end": TEST_NOW.isoformat()},
        }
    ]


def test_investigate_fraud_lists_the_evidence_refs_in_order(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    refs = [_as_dict(item)["ref"] for item in _as_list(body["evidence"])]
    assert refs == [
        f"episode:{seeded.own_ids[0]}",
        f"alert:{seeded.alert_id}",
        *[f"payment:{seeded.own_ids[index]}" for index in (11, 10, 9, 8, 7)],
    ]


def test_investigate_fraud_sends_the_response_with_cache_control_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_investigate_fraud_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_analysis(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    with_llm = _post_ok(
        _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm()), key_pair
    )
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, DownLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    assert body["cause_label"] == "card_testing"
    assert {k: v for k, v in body.items() if k not in ("mode", "text", "model")} == {
        k: v for k, v in with_llm.items() if k not in ("mode", "text", "model")
    }


def test_investigate_fraud_for_an_unsupported_question_returns_the_empty_shape_with_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        ROUTE, json={"question": "Hello"}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "intent": "unsupported",
        "cause_label": "unsupported",
        "mode": "t2_fallback",
        "text": None,
        "model": None,
        "analysis": None,
        "alerts": [],
        "suggested_fixes": [],
        "tool_calls": [],
        "evidence": [],
    }
    assert llm.calls == []


def test_investigate_fraud_without_a_flagged_episode_returns_no_card_testing_and_empty_lists(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange: a merchant with no payments at all
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair, merchant_id=THIRD_MERCHANT)

    # Assert
    assert (body["intent"], body["cause_label"]) == ("card_testing", "no_card_testing")
    assert (body["alerts"], body["suggested_fixes"], body["evidence"]) == ([], [], [])
    assert _as_dict(body["analysis"])["episodes"] == []


def test_investigate_fraud_scopes_everything_to_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    own = _post_ok(client, key_pair, merchant_id=MERCHANT)
    others = _post_ok(client, key_pair, merchant_id=OTHER_MERCHANT)

    # Assert
    assert _episode(own)["first_payment_id"] == str(seeded.own_ids[0])
    assert _episode(others)["first_payment_id"] == str(seeded.others_ids[0])
    assert _as_list(others["alerts"]) == [
        {
            "alert_id": str(seeded.others_alert_id),
            "ticket_id": str(seeded.others_ticket_id),
            "queue": "fraud",
            "first_payment_id": str(seeded.others_ids[0]),
        }
    ]
    assert str(seeded.others_alert_id) not in str(own)
    assert [str(i) for i in seeded.others_ids if str(i) in str(own)] == []


def test_investigate_fraud_accepts_a_question_of_exactly_2000_characters(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE, json={"question": "x" * 2000}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200


# --- auth -------------------------------------------------------------------------------------------------


def test_investigate_fraud_rejects_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer garbage-token-canary-8842"}, {"Authorization": "Basic Z2FyYmFnZQ=="}],
    ids=["no_authorization_header", "garbage_token", "not_a_bearer_scheme"],
)
def test_investigate_fraud_without_a_valid_token_returns_401_invalid_token(
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
    assert "garbage-token-canary-8842" not in response.text
    assert llm.calls == []


def test_investigate_fraud_rejects_a_merchant_token_signed_by_another_key_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, other_key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_merchant_token(other_key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


def test_investigate_fraud_rejects_an_expired_merchant_token_once_the_injected_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW + timedelta(seconds=901), key_pair.public_pem, RecordingLlm()
    )

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


# --- body validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["merchant_id", "user_id", "start", "limit"])
def test_investigate_fraud_rejects_any_extra_body_field_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, field_name: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    body = {"question": QUESTION, field_name: str(OTHER_MERCHANT)}

    # Act
    response = client.post(ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert str(OTHER_MERCHANT) not in response.text
    assert llm.calls == []


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "x" * 2001}, {"question": 123}, {"question": None}],
    ids=["no_question", "empty_question", "question_2001_chars", "question_not_a_string", "question_null"],
)
def test_investigate_fraud_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert llm.calls == []
