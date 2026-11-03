"""Tests for POST /v1/assistant/explain-settlement after the security review (docs/specs/sq4.md H1, H2)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ROUTE = "/v1/assistant/explain-settlement"
QUESTION = "Why is my October payout lower than expected?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
CAPTURED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)

INSERT_ENTRY = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, 'seed')"
)
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :method, 'succeeded', 1, :occurred_at)"
)

# Two USD 5.00 payments, one charged back: gross 1000, mdr -29, fixed fees -60, chargebacks -500,
# chargeback fees -1500, reserve -50 -> net -1139, expected 971, gap 2110.
NEGATIVE: tuple[tuple[int, str, bool, bool], ...] = ((500, "USD", False, False), (500, "USD", False, True))
NEGATIVE_LINES = [
    {"kind": "gross", "amount_minor": 1_000, "count": 2},
    {"kind": "mdr", "amount_minor": -29, "count": 2},
    {"kind": "fixed_fees", "amount_minor": -60, "count": 2},
    {"kind": "fx", "amount_minor": 0, "count": 0},
    {"kind": "refunds", "amount_minor": 0, "count": 0},
    {"kind": "chargebacks", "amount_minor": -500, "count": 1},
    {"kind": "chargeback_fees", "amount_minor": -1_500, "count": 1},
    {"kind": "reserve", "amount_minor": -50, "count": 2},
]
NEGATIVE_SUMMARY = (
    "Your 2026-10 settlement has no payout: you owe 11.39 USD, 21.10 USD less than the 9.71 USD you would "
    "expect from gross sales of 10.00 USD minus the 2.90% MDR. "
    "Fixed fees: 0.60 USD (2 payments at 0.30 USD each). "
    "Chargebacks: 5.00 USD on 1 payments. "
    "Chargeback fees: 15.00 USD (1 at 15.00 USD each). "
    "Rolling reserve: 0.50 USD held (5.00% of gross), released in a later payout."
)
NEGATIVE_STATEMENT_EVIDENCE = (
    "Settlement statement 2026-10 in USD: gross 10.00 USD, expected after MDR 9.71 USD, "
    "net balance owed by you 11.39 USD, gap 21.10 USD"
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


def _load(
    session_factory: sessionmaker[Session],
    rows: tuple[tuple[int, str, bool, bool], ...],
    *,
    method: str = "card",
) -> None:
    with session_factory.begin() as session:
        for offset, (amount_minor, currency, refunded, chargeback) in enumerate(rows):
            payment_id = uuid.uuid4()
            captured_at = CAPTURED_AT + timedelta(minutes=offset)
            session.execute(
                INSERT_ENTRY,
                {
                    "payment_id": payment_id,
                    "merchant_id": MERCHANT,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "captured_at": captured_at,
                    "statement_period": "2026-10",
                    "refunded": refunded,
                    "chargeback": chargeback,
                },
            )
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": MERCHANT,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "method": method,
                    "occurred_at": captured_at,
                },
            )


def _client(
    session_factory: sessionmaker[Session], clock: Callable[[], datetime], public_key_pem: str, llm: LlmPort
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=llm, jwt_public_key_pem=public_key_pem, clock=clock
    )
    return TestClient(create_app(deps))


def _merchant_token(private_pem: str) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT}",
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        "merchant_id": str(MERCHANT),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _post(client: TestClient, key_pair: KeyPair) -> Response:
    response: Response = client.post(
        ROUTE,
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {_merchant_token(key_pair.private_pem)}"},
    )
    return response


# --- H1: a negative net over HTTP --------------------------------------------------------------------------


def test_explain_settlement_returns_a_signed_negative_net_with_the_no_payout_summary(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, key_pair)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert {
        k: body[k]
        for k in (
            "lines",
            "gross_minor",
            "expected_minor",
            "net_minor",
            "gap_minor",
            "verified",
            "mismatches",
        )
    } == {
        "lines": NEGATIVE_LINES,
        "gross_minor": 1_000,
        "expected_minor": 971,
        "net_minor": -1_139,
        "gap_minor": 2_110,
        "verified": True,
        "mismatches": [],
    }
    assert body["summary"] == NEGATIVE_SUMMARY


def test_explain_settlement_negative_net_statement_evidence_says_balance_owed_not_payout(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post(client, key_pair).json()

    # Assert
    evidence = {item["ref"]: item["summary"] for item in body["evidence"]}
    assert evidence["statement:2026-10"] == NEGATIVE_STATEMENT_EVIDENCE
    assert [item["ref"] for item in body["evidence"] if "net payout" in item["summary"]] == []


# --- H2: a non-card projection row over HTTP ---------------------------------------------------------------


def test_explain_settlement_with_a_wallet_projection_row_is_unverified_and_skips_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE, method="wallet")
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = _post(client, key_pair)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert (body["verified"], body["mismatches"], body["mode"], body["text"], body["model"]) == (
        False,
        ["projection_mismatch"],
        "t2_fallback",
        None,
        None,
    )
    assert body["summary"] == (
        "The 2026-10 settlement could not be verified (projection_mismatch); "
        "contact support with reference 2026-10."
    )
    assert llm.calls == []
