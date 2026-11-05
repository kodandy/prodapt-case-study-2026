"""Tests for POST /v1/agents/investigate, GET /v1/agents/messages/{correlation_id} and
POST /v1/agents/transfer-rca, written from docs/specs/r2-agents.md section 5."""

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

INVESTIGATE = "/v1/agents/investigate"
MESSAGES = "/v1/agents/messages"
RCA = "/v1/agents/transfer-rca"
QUESTION = "Is this card testing?"
RCA_QUESTION = "My international transfer was declined even though I had enough money - what triggered it?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
NOT_FOUND_BODY = {"detail": "transfer not found"}
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
BURST_START = TEST_NOW - timedelta(hours=2)
DECIDED_AT = TEST_NOW - timedelta(hours=3)
XB_RULE = "xb_limit_kyc_tier1_v3"
XB_REASON = "cross_border_limit_kyc_tier"
INVESTIGATE_KEYS = {
    "correlation_id",
    "plan",
    "finding",
    "contributions",
    "messages",
    "evidence",
    "truncated",
    "mode",
    "text",
    "model",
}
RCA_KEYS = {
    "correlation_id",
    "cause_code",
    "remediation_owner",
    "contributions",
    "messages",
    "suggested_fixes",
    "evidence",
    "truncated",
    "mode",
    "text",
    "model",
}
CONTRIBUTION_KEYS = {"agent", "status", "summary", "evidence", "tool_calls"}
MESSAGE_KEYS = {"sequence", "sender", "recipient", "intent", "payload", "evidence_refs"}

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, 50, 'USD', 'card', :state, 'acq_a', NULL, '400000', :card_ref, 1, "
    ":occurred_at)"
)
INSERT_TRANSFER = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, :amount_minor, 'USD', 'bank_transfer', 'failed', 'US', 'IN', "
    ":reason_code, :rule_id, 1, 100000, 'USD', 1000000, 525000, :decided_at, 'seed')"
)
INSERT_KYC = text(
    "INSERT INTO ops_ai.kyc_profiles (user_id, kyc_status, country, ingest_source) VALUES "
    "(:user_id, 'verified', 'US', 'seed')"
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
    others_failed: uuid.UUID
    first_payment: uuid.UUID


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


def _insert_transfer(session_factory: sessionmaker[Session], sender: uuid.UUID, amount: int) -> uuid.UUID:
    transfer_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_TRANSFER,
            {
                "transfer_id": transfer_id,
                "sender_user_id": sender,
                "amount_minor": amount,
                "reason_code": XB_REASON,
                "rule_id": XB_RULE,
                "decided_at": DECIDED_AT,
            },
        )
    return transfer_id


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    """A flagged card-testing burst of the merchant, a failed SQ2 transfer of the sender, and another
    sender's failed transfer."""
    ids = [uuid.UUID(int=1 + index) for index in range(12)]
    with session_factory.begin() as session:
        for index, payment_id in enumerate(ids):
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": MERCHANT,
                    "state": "declined" if index < 6 else "succeeded",
                    "card_ref": f"{index % 4 + 1:032x}",
                    "occurred_at": BURST_START + timedelta(seconds=10 * index),
                },
            )
        session.execute(INSERT_KYC, {"user_id": SENDER})
    own_failed = _insert_transfer(session_factory, SENDER, 180_000)
    others_failed = _insert_transfer(session_factory, OTHER_SENDER, 777_700)
    return Seeded(own_failed=own_failed, others_failed=others_failed, first_payment=ids[0])


def _client_for(session_factory: sessionmaker[Session], key_pair: KeyPair, llm: LlmPort) -> TestClient:
    return _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)


def _investigate_ok(
    client: TestClient, key_pair: KeyPair, merchant_id: uuid.UUID = MERCHANT
) -> dict[str, object]:
    response = client.post(
        INVESTIGATE,
        json={"question": QUESTION},
        headers=_bearer(_merchant_token(key_pair.private_pem, merchant_id)),
    )
    assert response.status_code == 200
    parsed: dict[str, object] = response.json()
    return parsed


def _rca_ok(
    client: TestClient,
    key_pair: KeyPair,
    body: dict[str, object] | None = None,
    user_id: uuid.UUID = SENDER,
) -> dict[str, object]:
    response = client.post(
        RCA,
        json=body if body is not None else {"question": RCA_QUESTION},
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


# --- POST /v1/agents/investigate -------------------------------------------------------------------------


def test_investigate_returns_exactly_the_specified_keys_with_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(
        INVESTIGATE, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == INVESTIGATE_KEYS
    assert body["plan"] == ["fraud"]
    assert body["truncated"] is False
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    uuid.UUID(body["correlation_id"])


def test_investigate_contributions_and_messages_have_their_own_key_sets(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    body = _investigate_ok(client, key_pair)

    # Assert
    contributions = body["contributions"]
    messages = body["messages"]
    assert isinstance(contributions, list)
    assert isinstance(messages, list)
    assert {c["agent"] for c in contributions} >= {"fraud"}
    for contribution in contributions:
        assert set(contribution) == CONTRIBUTION_KEYS
        assert all(set(item) == {"ref", "summary"} for item in contribution["evidence"])
        assert all(set(call) == {"name", "args"} for call in contribution["tool_calls"])
    assert [m["sequence"] for m in messages] == list(range(1, len(messages) + 1))
    for message in messages:
        assert set(message) == MESSAGE_KEYS
    first = messages[0]
    assert (first["sender"], first["recipient"], first["intent"]) == ("supervisor", "fraud", "investigate")


def test_investigate_reports_the_fraud_finding_and_its_episode_evidence(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    body = _investigate_ok(client, key_pair)

    # Assert
    contributions = body["contributions"]
    assert isinstance(contributions, list)
    fraud = next(c for c in contributions if c["agent"] == "fraud")
    assert fraud["status"] == "finding"
    assert [item["ref"] for item in fraud["evidence"]] == [f"episode:{seeded.first_payment}"]
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert all(set(item) == {"ref", "summary"} for item in evidence)
    assert f"episode:{seeded.first_payment}" in [item["ref"] for item in evidence]


def test_investigate_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_plan(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    with_llm = _investigate_ok(_client_for(session_factory, key_pair, RecordingLlm()), key_pair)
    down = DownLlm()

    # Act
    fallback = _investigate_ok(_client_for(session_factory, key_pair, down), key_pair)

    # Assert
    assert (fallback["mode"], fallback["text"], fallback["model"]) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert fallback["plan"] == with_llm["plan"]
    assert fallback["finding"] == with_llm["finding"]
    assert fallback["evidence"] == with_llm["evidence"]


def test_investigate_scopes_the_investigation_to_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    body = _investigate_ok(client, key_pair, OTHER_MERCHANT)

    # Assert
    contributions = body["contributions"]
    assert isinstance(contributions, list)
    assert next(c for c in contributions if c["agent"] == "fraud")["status"] == "no_finding"
    assert body["evidence"] == []
    assert str(seeded.first_payment) not in str(body)


@pytest.mark.parametrize("field_name", ["merchant_id", "user_id"])
def test_investigate_rejects_a_caller_chosen_identity_field_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, field_name: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client_for(session_factory, key_pair, llm)

    # Act
    response = client.post(
        INVESTIGATE,
        json={"question": QUESTION, field_name: str(OTHER_MERCHANT)},
        headers=_bearer(_merchant_token(key_pair.private_pem)),
    )

    # Assert
    assert response.status_code == 422
    assert llm.calls == []


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "x" * 2001}, {"question": 123}],
    ids=["no_question", "empty_question", "question_2001_chars", "question_not_a_string"],
)
def test_investigate_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(INVESTIGATE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422


def test_investigate_rejects_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(
        INVESTIGATE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_investigate_without_a_token_returns_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(INVESTIGATE, json={"question": QUESTION})

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


# --- GET /v1/agents/messages/{correlation_id} ------------------------------------------------------------


def test_get_messages_returns_the_persisted_trail_of_the_investigation_with_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())
    investigated = _investigate_ok(client, key_pair)

    # Act
    response = client.get(
        f"{MESSAGES}/{investigated['correlation_id']}",
        headers=_bearer(_merchant_token(key_pair.private_pem)),
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"messages": investigated["messages"]}


def test_get_messages_of_another_merchant_is_the_same_404_as_an_unknown_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())
    investigated = _investigate_ok(client, key_pair)
    headers = _bearer(_merchant_token(key_pair.private_pem, OTHER_MERCHANT))

    # Act
    others = client.get(f"{MESSAGES}/{investigated['correlation_id']}", headers=headers)
    missing = client.get(f"{MESSAGES}/{uuid.uuid4()}", headers=headers)

    # Assert
    assert others.status_code == missing.status_code == 404
    assert others.content == missing.content
    assert str(investigated["correlation_id"]) not in others.text


def test_get_messages_of_an_unknown_correlation_id_returns_404(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.get(
        f"{MESSAGES}/{uuid.uuid4()}", headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 404


def test_get_messages_rejects_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())
    investigated = _investigate_ok(client, key_pair)

    # Act
    response = client.get(
        f"{MESSAGES}/{investigated['correlation_id']}", headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_get_messages_without_a_token_returns_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.get(f"{MESSAGES}/{uuid.uuid4()}")

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


# --- POST /v1/agents/transfer-rca ------------------------------------------------------------------------


def test_transfer_rca_returns_exactly_the_specified_keys_with_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(
        RCA, json={"question": RCA_QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == RCA_KEYS
    assert (body["cause_code"], body["remediation_owner"]) == ("cross_border_limit_for_kyc_tier", "sender")
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)
    assert body["truncated"] is False
    assert len(body["suggested_fixes"]) == 1
    assert "tier 2" in body["suggested_fixes"][0].lower()
    uuid.UUID(body["correlation_id"])


def test_transfer_rca_returns_the_six_message_trail_and_the_three_contributions(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    body = _rca_ok(client, key_pair, {"question": RCA_QUESTION, "transfer_id": str(seeded.own_failed)})

    # Assert
    messages = body["messages"]
    contributions = body["contributions"]
    assert isinstance(messages, list)
    assert isinstance(contributions, list)
    trail = [(m["sequence"], m["sender"], m["recipient"], m["intent"]) for m in messages]
    assert trail == [
        (1, "supervisor", "fraud", "assess_transfer"),
        (2, "fraud", "compliance", "check_kyc"),
        (3, "fraud", "supervisor", "finding"),
        (4, "compliance", "settlement", "verify_funds"),
        (5, "settlement", "compliance", "funds_verified"),
        (6, "compliance", "supervisor", "root_cause"),
    ]
    assert all(set(m) == MESSAGE_KEYS for m in messages)
    assert messages[0]["payload"] == {"transfer_id": str(seeded.own_failed)}
    assert messages[4]["payload"] == {"balance_covered_amount": True}
    assert [c["agent"] for c in contributions] == ["fraud", "compliance", "settlement"]
    assert all(set(c) == CONTRIBUTION_KEYS for c in contributions)


def test_transfer_rca_without_a_transfer_id_uses_the_newest_failed_transfer_of_the_sender(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    body = _rca_ok(client, key_pair)

    # Assert
    messages = body["messages"]
    assert isinstance(messages, list)
    assert messages[0]["payload"] == {"transfer_id": str(seeded.own_failed)}
    assert str(seeded.others_failed) not in str(body)


def test_transfer_rca_never_exposes_the_rule_id_or_the_reason_code(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())
    response = client.post(
        RCA, json={"question": RCA_QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Act
    keys = _keys_everywhere(response.json())

    # Assert
    assert response.status_code == 200
    assert {"rule_id", "reason_code"} & keys == set()
    assert XB_RULE not in response.text
    assert XB_REASON not in response.text


def test_transfer_rca_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_findings(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    with_llm = _rca_ok(_client_for(session_factory, key_pair, RecordingLlm()), key_pair)
    down = DownLlm()

    # Act
    fallback = _rca_ok(_client_for(session_factory, key_pair, down), key_pair)

    # Assert
    assert (fallback["mode"], fallback["text"], fallback["model"]) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert fallback["cause_code"] == with_llm["cause_code"]
    assert fallback["suggested_fixes"] == with_llm["suggested_fixes"]
    assert fallback["evidence"] == with_llm["evidence"]


def test_transfer_rca_gives_the_identical_404_for_another_senders_transfer_and_a_missing_one(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client_for(session_factory, key_pair, llm)
    headers = _bearer(_user_token(key_pair.private_pem))

    # Act
    others = client.post(
        RCA, json={"question": RCA_QUESTION, "transfer_id": str(seeded.others_failed)}, headers=headers
    )
    missing = client.post(
        RCA, json={"question": RCA_QUESTION, "transfer_id": str(uuid.uuid4())}, headers=headers
    )

    # Assert
    assert others.status_code == missing.status_code == 404
    assert others.json() == missing.json() == NOT_FOUND_BODY
    assert others.content == missing.content
    assert str(seeded.others_failed) not in others.text
    assert llm.calls == []


def test_transfer_rca_rejects_a_merchant_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client_for(session_factory, key_pair, llm)

    # Act
    response = client.post(
        RCA, json={"question": RCA_QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


def test_transfer_rca_without_a_token_returns_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(RCA, json={"question": RCA_QUESTION})

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize("field_name", ["user_id", "sender_user_id", "merchant_id", "limit"])
def test_transfer_rca_rejects_an_extra_field_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, field_name: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client_for(session_factory, key_pair, llm)

    # Act
    response = client.post(
        RCA,
        json={"question": RCA_QUESTION, field_name: str(OTHER_SENDER)},
        headers=_bearer(_user_token(key_pair.private_pem)),
    )

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
        {"question": RCA_QUESTION, "transfer_id": "not-a-uuid"},
        {"question": RCA_QUESTION, "transfer_id": 12345},
    ],
    ids=[
        "no_question",
        "empty_question",
        "question_2001_chars",
        "transfer_id_not_a_uuid",
        "transfer_id_numeric",
    ],
)
def test_transfer_rca_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded, body: object
) -> None:
    # Arrange
    client = _client_for(session_factory, key_pair, RecordingLlm())

    # Act
    response = client.post(RCA, json=body, headers=_bearer(_user_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
