"""Route tests for docs/specs/sq2.md sections 9.1 and 9.5: the sender-facing status and no-store caching."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, get_args, get_origin

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, TransferOut, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ROUTE = "/v1/assistant/investigate-transfer"
QUESTION = "My international transfer was held even though I had enough money - what happened?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
DECIDED_AT = TEST_NOW - timedelta(hours=3)
FORBIDDEN_WORDS = ("flagged", "fraud", "review")
LEAKY_RULE_ID = "fraud_model_review_v2"

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


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
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
        "rule_id": "xb_limit_kyc_tier1_v3",
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


def _token(private_pem: str, claims: dict[str, object]) -> str:
    issued_at = int(TEST_NOW.timestamp())
    base: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode({**base, **claims}, private_pem, algorithm="RS256")


def _user_headers(private_pem: str) -> dict[str, str]:
    token = _token(private_pem, {"sub": f"user:{SENDER}", "user_id": str(SENDER), "roles": ["consumer"]})
    return {"Authorization": f"Bearer {token}"}


def _merchant_headers(private_pem: str) -> dict[str, str]:
    token = _token(
        private_pem,
        {"sub": f"merchant:{MERCHANT}", "merchant_id": str(MERCHANT), "roles": ["merchant_admin"]},
    )
    return {"Authorization": f"Bearer {token}"}


# --- 9.1 sender-facing status ----------------------------------------------------------------------------


def test_transfer_out_status_is_typed_as_the_four_sender_facing_values() -> None:
    # Arrange / Act
    annotation = TransferOut.model_fields["status"].annotation

    # Assert
    assert get_origin(annotation) is Literal
    assert set(get_args(annotation)) == {"success", "failed", "pending", "reversed"}


def test_a_flagged_transfer_is_reported_as_pending_and_classified_not_declined(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status="flagged", reason_code=None, rule_id=LEAKY_RULE_ID)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE,
        json={"question": QUESTION, "transfer_id": str(transfer_id)},
        headers=_user_headers(key_pair.private_pem),
    )

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert body["transfer"]["status"] == "pending"
    assert (body["cause_code"], body["remediation_owner"]) == ("not_declined", "unknown")
    assert body["evidence"][0] == {
        "ref": f"transfer:{transfer_id}",
        "summary": f"Transfer of 1800.00 USD from US to IN, pending at {DECIDED_AT.isoformat()}",
    }


def test_a_flagged_transfer_response_contains_none_of_the_forbidden_words(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status="flagged", reason_code=None, rule_id=LEAKY_RULE_ID)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE,
        json={"question": QUESTION, "transfer_id": str(transfer_id)},
        headers=_user_headers(key_pair.private_pem),
    )

    # Assert
    assert response.status_code == 200
    assert [word for word in FORBIDDEN_WORDS if word in response.text.lower()] == []


def test_a_flagged_transfer_llm_request_contains_none_of_the_forbidden_words(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status="flagged", reason_code=None, rule_id=LEAKY_RULE_ID)
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        ROUTE,
        json={"question": QUESTION, "transfer_id": str(transfer_id)},
        headers=_user_headers(key_pair.private_pem),
    )

    # Assert
    assert response.status_code == 200
    assert len(llm.calls) == 1
    assert [word for word in FORBIDDEN_WORDS if word in repr(llm.calls[0]).lower()] == []


def test_a_flagged_transfer_t2_fallback_response_contains_none_of_the_forbidden_words(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status="flagged", reason_code=None, rule_id=LEAKY_RULE_ID)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, DownLlm())

    # Act
    response = client.post(
        ROUTE,
        json={"question": QUESTION, "transfer_id": str(transfer_id)},
        headers=_user_headers(key_pair.private_pem),
    )

    # Assert
    body = response.json()
    assert (response.status_code, body["mode"], body["transfer"]["status"]) == (200, "t2_fallback", "pending")
    assert [word for word in FORBIDDEN_WORDS if word in response.text.lower()] == []


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("success", "success"),
        ("failed", "failed"),
        ("pending", "pending"),
        ("reversed", "reversed"),
        ("flagged", "pending"),
        ("on_hold", "pending"),
    ],
)
def test_the_response_status_is_the_sender_facing_value_of_the_raw_status(
    session_factory: sessionmaker[Session], key_pair: KeyPair, raw: str, shown: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status=raw)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE,
        json={"question": QUESTION, "transfer_id": str(transfer_id)},
        headers=_user_headers(key_pair.private_pem),
    )

    # Assert
    assert response.status_code == 200
    assert response.json()["transfer"]["status"] == shown


# --- 9.5 caching -----------------------------------------------------------------------------------------


def test_a_200_response_forbids_caching(session_factory: sessionmaker[Session], key_pair: KeyPair) -> None:
    # Arrange
    _insert(session_factory)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_user_headers(key_pair.private_pem))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_a_t2_fallback_200_response_forbids_caching(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _insert(session_factory)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, DownLlm())

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_user_headers(key_pair.private_pem))

    # Assert
    assert (response.status_code, response.json()["mode"]) == (200, "t2_fallback")
    assert response.headers["Cache-Control"] == "no-store"


def test_a_404_response_carries_no_cache_control(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=_user_headers(key_pair.private_pem))

    # Assert
    assert response.status_code == 404
    assert "cache-control" not in response.headers


def test_a_403_response_carries_no_cache_control(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _insert(session_factory)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_merchant_headers(key_pair.private_pem)
    )

    # Assert
    assert response.status_code == 403
    assert "cache-control" not in response.headers


def test_a_422_response_carries_no_cache_control(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _insert(session_factory)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(ROUTE, json={"question": ""}, headers=_user_headers(key_pair.private_pem))

    # Assert
    assert response.status_code == 422
    assert "cache-control" not in response.headers
