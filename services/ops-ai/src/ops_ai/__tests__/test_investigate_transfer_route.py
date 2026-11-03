"""Tests for POST /v1/assistant/investigate-transfer, written from docs/specs/sq2.md sections 2 and 7."""

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

ROUTE = "/v1/assistant/investigate-transfer"
QUESTION = "My international transfer was declined even though I had enough money - what triggered it?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
NOT_FOUND_BODY = {"detail": "transfer not found"}
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
DECIDED_AT = TEST_NOW - timedelta(hours=3)
XB_RULE = "xb_limit_kyc_tier1_v3"
TOP_LEVEL_KEYS = {
    "cause_code",
    "remediation_owner",
    "mode",
    "text",
    "model",
    "transfer",
    "balance_covered_amount",
    "suggested_fixes",
    "tool_calls",
    "evidence",
}
TRANSFER_KEYS = {
    "transfer_id",
    "amount_minor",
    "currency",
    "method",
    "status",
    "country_sender",
    "country_receiver",
    "kyc_tier",
    "limit_minor",
    "limit_currency",
    "next_tier_limit_minor",
    "sender_balance_minor",
    "decided_at",
}

INSERT = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, :amount_minor, :currency, :method, :status, :country_sender, "
    ":country_receiver, :reason_code, :rule_id, :kyc_tier, :limit_minor, :limit_currency, "
    ":next_tier_limit_minor, :sender_balance_minor, :decided_at, :ingest_source)"
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
    own_failed: uuid.UUID
    own_success: uuid.UUID
    others_failed: uuid.UUID


def _insert(session_factory: sessionmaker[Session], **overrides: object) -> uuid.UUID:
    params: dict[str, object] = {
        "transfer_id": uuid.uuid4(),
        "sender_user_id": SENDER,
        "amount_minor": 180_000,
        "currency": "USD",
        "method": "bank_transfer",
        "status": "failed",
        "country_sender": "US",
        "country_receiver": "IN",
        "reason_code": "cross_border_limit_kyc_tier",
        "rule_id": XB_RULE,
        "kyc_tier": 1,
        "limit_minor": 100_000,
        "limit_currency": "USD",
        "next_tier_limit_minor": 1_000_000,
        "sender_balance_minor": 525_000,
        "decided_at": DECIDED_AT,
        "ingest_source": "seed",
    }
    params.update(overrides)
    with session_factory.begin() as session:
        session.execute(INSERT, params)
    transfer_id = params["transfer_id"]
    assert isinstance(transfer_id, uuid.UUID)
    return transfer_id


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    """The sender has a failed limit transfer (newest failed) and an older success; another sender has a
    newer failed transfer of its own."""
    own_success = _insert(
        session_factory,
        status="success",
        reason_code=None,
        rule_id=None,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
        decided_at=DECIDED_AT + timedelta(minutes=30),
    )
    own_failed = _insert(session_factory)
    others_failed = _insert(
        session_factory,
        sender_user_id=OTHER_SENDER,
        amount_minor=777_700,
        sender_balance_minor=9_000_000,
        decided_at=DECIDED_AT + timedelta(hours=1),
    )
    return Seeded(own_failed=own_failed, own_success=own_success, others_failed=others_failed)


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


def _user_token(private_pem: str, user_id: uuid.UUID = SENDER) -> str:
    claims = _claims(f"user:{user_id}", {"user_id": str(user_id)}, ["consumer"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _merchant_token(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post_ok(
    client: TestClient, key_pair: KeyPair, body: dict[str, object] | None = None, user_id: uuid.UUID = SENDER
) -> dict[str, object]:
    response = client.post(
        ROUTE,
        json=body if body is not None else {"question": QUESTION},
        headers=_bearer(_user_token(key_pair.private_pem, user_id)),
    )
    assert response.status_code == 200
    parsed: dict[str, object] = response.json()
    return parsed


def _keys_everywhere(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(key) for key in value} | {k for item in value.values() for k in _keys_everywhere(item)}
    if isinstance(value, list):
        return {k for item in value for k in _keys_everywhere(item)}
    return set()


# --- success shape ---------------------------------------------------------------------------------------


def test_investigate_transfer_returns_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS
    transfer = body["transfer"]
    assert isinstance(transfer, dict)
    assert set(transfer) == TRANSFER_KEYS


def test_investigate_transfer_never_exposes_the_rule_id_or_the_reason_code(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Act
    keys = _keys_everywhere(response.json())

    # Assert
    assert response.status_code == 200
    assert {"rule_id", "reason_code"} & keys == set()
    assert XB_RULE not in response.text
    assert "xb_limit" not in response.text


def test_investigate_transfer_without_an_id_returns_the_newest_failed_transfer_of_the_sender(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    transfer = body["transfer"]
    assert isinstance(transfer, dict)
    assert transfer["transfer_id"] == str(seeded.own_failed)
    assert {k: transfer[k] for k in TRANSFER_KEYS - {"transfer_id", "decided_at"}} == {
        "amount_minor": 180_000,
        "currency": "USD",
        "method": "bank_transfer",
        "status": "failed",
        "country_sender": "US",
        "country_receiver": "IN",
        "kyc_tier": 1,
        "limit_minor": 100_000,
        "limit_currency": "USD",
        "next_tier_limit_minor": 1_000_000,
        "sender_balance_minor": 525_000,
    }
    assert datetime.fromisoformat(str(transfer["decided_at"])) == DECIDED_AT


def test_investigate_transfer_returns_the_cause_the_owner_the_narration_and_the_fix(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert (body["cause_code"], body["remediation_owner"]) == ("cross_border_limit_for_kyc_tier", "sender")
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert body["balance_covered_amount"] is True
    fixes = body["suggested_fixes"]
    assert isinstance(fixes, list)
    assert len(fixes) == 1
    assert "tier 2" in str(fixes[0]).lower()


def test_investigate_transfer_lists_the_tool_call_and_the_evidence_with_their_own_key_sets(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert body["tool_calls"] == [{"name": "get_payment_status", "args": {"transfer_id": "latest_failed"}}]
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert all(set(item) == {"ref", "summary"} for item in evidence)
    assert [item["ref"] for item in evidence] == [
        f"transfer:{seeded.own_failed}",
        f"balance:{seeded.own_failed}",
        f"limit:{seeded.own_failed}",
    ]


def test_investigate_transfer_with_an_id_returns_that_transfer_even_if_it_did_not_fail(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair, {"question": QUESTION, "transfer_id": str(seeded.own_success)})

    # Assert
    transfer = body["transfer"]
    assert isinstance(transfer, dict)
    assert transfer["transfer_id"] == str(seeded.own_success)
    assert (body["cause_code"], body["remediation_owner"]) == ("not_declined", "unknown")
    assert body["tool_calls"] == [
        {"name": "get_payment_status", "args": {"transfer_id": str(seeded.own_success)}}
    ]


def test_investigate_transfer_scopes_the_default_transfer_to_the_token_user(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair, user_id=OTHER_SENDER)

    # Assert
    transfer = body["transfer"]
    assert isinstance(transfer, dict)
    assert transfer["transfer_id"] == str(seeded.others_failed)
    assert transfer["amount_minor"] == 777_700
    assert str(seeded.own_failed) not in str(body)


def test_investigate_transfer_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_analysis(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    with_llm = _post_ok(
        _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm()), key_pair
    )
    down = DownLlm()
    down_client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, down)

    # Act
    fallback = _post_ok(down_client, key_pair)

    # Assert
    assert (fallback["mode"], fallback["text"], fallback["model"]) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert {k: v for k, v in fallback.items() if k not in ("mode", "text", "model")} == {
        k: v for k, v in with_llm.items() if k not in ("mode", "text", "model")
    }


# --- not found -------------------------------------------------------------------------------------------


def test_investigate_transfer_gives_the_identical_404_for_another_senders_transfer_and_a_missing_one(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    headers = _bearer(_user_token(key_pair.private_pem))

    # Act
    others = client.post(
        ROUTE, json={"question": QUESTION, "transfer_id": str(seeded.others_failed)}, headers=headers
    )
    missing = client.post(
        ROUTE, json={"question": QUESTION, "transfer_id": str(uuid.uuid4())}, headers=headers
    )

    # Assert
    assert others.status_code == missing.status_code == 404
    assert others.json() == missing.json() == NOT_FOUND_BODY
    assert others.content == missing.content
    assert str(seeded.others_failed) not in others.text
    assert llm.calls == []


def test_investigate_transfer_returns_404_when_the_sender_has_no_failed_transfer(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    stranger = uuid.UUID("c3c3c3c3-0000-4000-8000-000000000003")

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem, stranger))
    )

    # Assert
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


# --- who may call it -------------------------------------------------------------------------------------


def test_investigate_transfer_rejects_a_merchant_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


@pytest.mark.parametrize("route", ["/v1/assistant/ask", "/v1/assistant/investigate"])
def test_merchant_routes_reject_a_valid_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, route: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        route, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


@pytest.mark.parametrize("route", ["/v1/assistant/ask", "/v1/assistant/investigate"])
def test_merchant_routes_still_accept_a_merchant_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, route: str
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        route, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200


# --- invalid tokens --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer garbage-token-canary-8842"}, {"Authorization": "Basic Z2FyYmFnZQ=="}],
    ids=["no_authorization_header", "garbage_token", "not_a_bearer_scheme"],
)
def test_investigate_transfer_without_a_valid_token_returns_401_invalid_token(
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


def test_investigate_transfer_rejects_a_user_token_signed_by_another_key_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, other_key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(other_key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


def test_investigate_transfer_rejects_an_expired_user_token_once_the_injected_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW + timedelta(seconds=901), key_pair.public_pem, RecordingLlm()
    )

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize(
    "claims",
    [
        _claims(f"user:{SENDER}", {"user_id": str(SENDER), "merchant_id": str(MERCHANT)}, ["consumer"]),
        _claims(f"user:{SENDER}", {"user_id": str(SENDER)}, ["merchant_admin"]),
        _claims(f"user:{OTHER_SENDER}", {"user_id": str(SENDER)}, ["consumer"]),
        _claims(f"user:{SENDER}", {}, ["consumer"]),
    ],
    ids=["both_ids", "wrong_role", "sub_mismatch", "no_id"],
)
def test_investigate_transfer_rejects_a_malformed_user_token_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, claims: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())
    token = jwt.encode(claims, key_pair.private_pem, algorithm="RS256")

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


# --- body validation -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["user_id", "sender_user_id", "merchant_id"])
def test_investigate_transfer_rejects_a_caller_chosen_identity_field_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, field_name: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    body = {"question": QUESTION, field_name: str(OTHER_SENDER)}

    # Act
    response = client.post(ROUTE, json=body, headers=_bearer(_user_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert str(seeded.others_failed) not in response.text
    assert llm.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"question": ""},
        {"question": "x" * 2001},
        {"question": 123},
        {"question": QUESTION, "transfer_id": "not-a-uuid"},
        {"question": QUESTION, "transfer_id": 12345},
        {"question": QUESTION, "limit": 5},
    ],
    ids=[
        "no_question",
        "empty_question",
        "question_2001_chars",
        "question_not_a_string",
        "transfer_id_not_a_uuid",
        "transfer_id_numeric",
        "extra_field",
    ],
)
def test_investigate_transfer_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(ROUTE, json=body, headers=_bearer(_user_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422


def test_investigate_transfer_accepts_a_question_of_exactly_2000_characters(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE, json={"question": "x" * 2000}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
