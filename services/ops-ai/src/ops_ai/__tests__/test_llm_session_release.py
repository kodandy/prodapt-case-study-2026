"""Tests for H1 of docs/specs/llm-adapter.md: no DB connection is held while the LLM is called.

A spy LlmPort records `engine.pool.checkedout()` inside `complete` for each of the five LLM routes.
"""

import uuid
from collections.abc import Callable
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
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from conftest import TEST_NOW

NARRATION = "narration from the pool spy"
MODEL = "pool-spy-model-1"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
WINDOW_START = TEST_NOW - timedelta(hours=6)
BURST_START = TEST_NOW - timedelta(hours=2)
CAPTURED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)

INSERT_BURST_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, 50, 'USD', 'card', :state, 'acq_a', NULL, '400000', :card_ref, 1, "
    ":occurred_at)"
)
INSERT_ENTRY = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, 'seed')"
)
INSERT_SETTLEMENT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, 'card', 'succeeded', 1, :occurred_at)"
)
INSERT_TRANSFER = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, 180000, 'USD', 'bank_transfer', 'failed', 'US', 'IN', "
    "'cross_border_limit_kyc_tier', 'xb_limit_kyc_tier1_v3', 1, 100000, 'USD', 1000000, 525000, "
    ":decided_at, 'seed')"
)
# USD cents after conversion: 10000, 10870, 6700, 5000, 2174, 1190 (a verified October statement)
MIXED: tuple[tuple[int, str, bool, bool], ...] = (
    (10_000, "USD", False, False),
    (10_000, "EUR", False, False),
    (10_000, "JPY", True, False),
    (5_000, "USD", False, True),
    (2_000, "EUR", True, True),
    (100_000, "INR", True, False),
)


@dataclass(frozen=True)
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


def _checked_out(engine: Engine) -> int:
    pool = engine.pool
    if not isinstance(pool, QueuePool):
        raise AssertionError(f"expected a QueuePool, got {type(pool).__name__}")
    return pool.checkedout()


@dataclass
class PoolSpyLlm:
    """Records the number of checked-out pool connections at the moment `complete` runs."""

    engine: Engine
    checked_out: list[int] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.checked_out.append(_checked_out(self.engine))
        return LlmResponse(text=NARRATION, model=MODEL)


def _payment_event(occurred_at: datetime, *, declined: bool) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=occurred_at,
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=MERCHANT,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0119" if declined else "4242",
    )


def _seed_payments(session_factory: sessionmaker[Session]) -> None:
    events = [
        _payment_event(WINDOW_START + timedelta(minutes=10 * (index + 1)), declined=True)
        for index in range(5)
    ]
    events += [
        _payment_event(WINDOW_START + timedelta(minutes=5 + index), declined=False) for index in range(5)
    ]
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)


def _seed_transfer(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_TRANSFER, {"transfer_id": uuid.uuid4(), "sender_user_id": SENDER, "decided_at": TEST_NOW}
        )


def _seed_burst(session_factory: sessionmaker[Session]) -> None:
    """12 micro card payments 10 s apart on 4 cards, the first 6 declined."""
    with session_factory.begin() as session:
        for index in range(12):
            session.execute(
                INSERT_BURST_FACT,
                {
                    "payment_id": uuid.UUID(int=index + 1),
                    "merchant_id": MERCHANT,
                    "state": "declined" if index < 6 else "succeeded",
                    "card_ref": f"{10 + index % 4 + 1:032x}",
                    "occurred_at": BURST_START + timedelta(seconds=index * 10),
                },
            )


def _seed_settlement(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        for offset, (amount_minor, currency, refunded, chargeback) in enumerate(MIXED):
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
                INSERT_SETTLEMENT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": MERCHANT,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "occurred_at": captured_at,
                },
            )


@dataclass(frozen=True)
class RouteCase:
    path: str
    question: str
    seed: Callable[[sessionmaker[Session]], None]
    as_sender: bool = False


ROUTE_CASES = [
    pytest.param(RouteCase("/v1/assistant/ask", "declined payments", _seed_payments), id="ask"),
    pytest.param(
        RouteCase(
            "/v1/assistant/investigate",
            "15% of my card payments are failing this afternoon - "
            "what is the root cause and how do we fix it?",
            _seed_payments,
        ),
        id="investigate",
    ),
    pytest.param(
        RouteCase(
            "/v1/assistant/investigate-transfer",
            "My international transfer was declined even though I had enough money - what triggered it?",
            _seed_transfer,
            as_sender=True,
        ),
        id="investigate-transfer",
    ),
    pytest.param(
        RouteCase(
            "/v1/assistant/investigate-fraud",
            "We're seeing a series of tiny card payments - is this card testing, and what should we do?",
            _seed_burst,
        ),
        id="investigate-fraud",
    ),
    pytest.param(
        RouteCase(
            "/v1/assistant/explain-settlement",
            "Why is my October payout lower than expected?",
            _seed_settlement,
        ),
        id="explain-settlement",
    ),
]


def _token(private_pem: str, *, as_sender: bool) -> str:
    issued_at = int(TEST_NOW.timestamp())
    if as_sender:
        subject: dict[str, object] = {"sub": f"user:{SENDER}", "user_id": str(SENDER), "roles": ["consumer"]}
    else:
        subject = {"sub": f"merchant:{MERCHANT}", "merchant_id": str(MERCHANT), "roles": ["merchant_admin"]}
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **subject,
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


@pytest.mark.parametrize("case", ROUTE_CASES)
def test_llm_route_holds_no_db_connection_during_the_llm_call_and_still_answers_in_llm_mode(
    engine: Engine, session_factory: sessionmaker[Session], key_pair: KeyPair, case: RouteCase
) -> None:
    # Arrange
    case.seed(session_factory)
    spy = PoolSpyLlm(engine)
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=spy,
        jwt_public_key_pem=key_pair.public_pem,
        clock=lambda: TEST_NOW,
    )
    client = TestClient(create_app(deps))
    token = _token(key_pair.private_pem, as_sender=case.as_sender)

    # Act
    response = client.post(
        case.path, json={"question": case.question}, headers={"Authorization": f"Bearer {token}"}
    )

    # Assert
    body = response.json()
    assert (response.status_code, body["mode"], body["text"], body["model"]) == (200, "llm", NARRATION, MODEL)
    assert spy.checked_out == [0]
