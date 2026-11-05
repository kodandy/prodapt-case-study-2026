"""POST /v1/assistant/investigate-fraud reports the applied fraud rule version, written from docs/specs/sq3.md
"Feedback-adjusted thresholds ... (11-06)" S-2."""

import uuid
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.clock import Clock
from ops_ai.feedback.thresholds import append_rule_version, thresholds_for_steps
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ROUTE = "/v1/assistant/investigate-fraud"
HEADER = "X-Fraud-Rule-Version"
QUESTION = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c11")
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, 50, 'USD', 'card', :state, 'acq_a', NULL, '400000', :card_ref, 1, "
    ":occurred_at)"
)


@pytest.fixture(scope="module")
def private_and_public_pem() -> tuple[str, str]:
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


def _burst(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        for index in range(12):
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": uuid.UUID(int=1 + index),
                    "merchant_id": MERCHANT,
                    "state": "declined" if index < 6 else "succeeded",
                    "card_ref": f"{index % 4 + 1:032x}",
                    "occurred_at": TEST_NOW - timedelta(hours=2) + timedelta(seconds=index * 10),
                },
            )


def _post(
    session_factory: sessionmaker[Session], clock: Clock, pems: tuple[str, str], question: str = QUESTION
) -> tuple[int, dict[str, str], dict[str, object]]:
    private_pem, public_pem = pems
    issued = int(TEST_NOW.timestamp())
    token = jwt.encode(
        {
            "iss": "gateway",
            "aud": "ops-ai",
            "sub": f"merchant:{MERCHANT}",
            "merchant_id": str(MERCHANT),
            "roles": ["merchant_admin"],
            "iat": issued,
            "exp": issued + 900,
            "jti": str(uuid.uuid4()),
        },
        private_pem,
        algorithm="RS256",
    )
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=MockLlm(), jwt_public_key_pem=public_pem, clock=clock
    )
    with TestClient(create_app(deps)) as client:
        response = client.post(
            ROUTE, json={"question": question}, headers={"Authorization": f"Bearer {token}"}
        )
    return response.status_code, dict(response.headers), response.json()


def test_route_returns_rule_version_zero_for_a_baseline_merchant(
    session_factory: sessionmaker[Session], clock: Clock, private_and_public_pem: tuple[str, str]
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    status, headers, body = _post(session_factory, clock, private_and_public_pem)

    # Assert
    assert status == 200
    assert headers[HEADER.lower()] == "0"
    assert body["cause_label"] == "card_testing"


def test_route_returns_the_active_version_and_its_verdict(
    session_factory: sessionmaker[Session], clock: Clock, private_and_public_pem: tuple[str, str]
) -> None:
    # Arrange
    _burst(session_factory)
    with session_factory.begin() as session:
        for steps in (1, 2):
            append_rule_version(
                session, Scope(MERCHANT), clock, steps, thresholds_for_steps(steps), "test:threshold", None
            )

    # Act
    status, headers, body = _post(session_factory, clock, private_and_public_pem)

    # Assert
    assert status == 200
    assert headers[HEADER.lower()] == "2"
    assert body["cause_label"] == "no_card_testing"
    assert body["evidence"] == []


def test_route_sends_no_rule_version_header_for_an_unsupported_question(
    session_factory: sessionmaker[Session], clock: Clock, private_and_public_pem: tuple[str, str]
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    status, headers, body = _post(
        session_factory, clock, private_and_public_pem, "What is our payout schedule?"
    )

    # Assert
    assert status == 200
    assert body["intent"] == "unsupported"
    assert HEADER.lower() not in headers
