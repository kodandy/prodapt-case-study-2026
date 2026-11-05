"""P0-11 log canary: secrets and personal data pushed through the full flow never reach any log record."""

import logging
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.outbox.relay import publish_pending
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.assistant.service import ask
from ops_ai.db import make_session_factory as make_ops_ai_session_factory
from ops_ai.ingest.consumer import consume_payments
from ops_ai.observability.llm import InstrumentedLlm
from ops_ai.scope import Scope
from redis import Redis
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

CANARY_EMAIL = "canary-7f3a@example.test"
CANARY_PASSWORD = "Canary-Pa55-7f3a!"  # noqa: S105 - canary value, not a secret
# 4000056655665556 is Luhn-valid but not on the gateway's test-card allowlist, so the canary is another
# allowlisted test PAN.
CANARY_PAN = "5555555555554444"
CANARY_CVC = "737"
QUESTION = "declined payments"


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _leaks(records: list[logging.LogRecord], canaries: dict[str, str]) -> list[tuple[str, str, str]]:
    """(canary name, logger name, where) for every canary found in a record's message or args."""
    found: list[tuple[str, str, str]] = []
    for record in records:
        views = {"message": record.getMessage(), "args": str(record.args), "msg": str(record.msg)}
        for where, view in views.items():
            for name, value in canaries.items():
                if value in view:
                    found.append((name, record.name, where))
    return found


def test_no_canary_secret_or_personal_data_appears_in_any_log_record_of_the_full_flow(
    session_factory: sessionmaker[Session],
    engine: Engine,
    redis_client: Redis,
    clock: FixedClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=redis_client,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    client = TestClient(
        create_app(
            GatewayAppDeps(
                charge=deps, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=_private_key_pem()
            )
        )
    )
    ops_ai_sessions = make_ops_ai_session_factory(engine)

    # Act
    signup = client.post("/v1/signup", json={"email": CANARY_EMAIL, "password": CANARY_PASSWORD})
    login = client.post("/v1/sessions", json={"email": CANARY_EMAIL, "password": CANARY_PASSWORD})
    session_token = login.json()["access_token"]
    wallets = client.get("/v1/me/wallets", headers={"Authorization": f"Bearer {session_token}"})
    payment = client.post(
        "/v1/payments",
        json={
            "amount_minor": 2500,
            "currency": "USD",
            "method": "card",
            "card": {"number": CANARY_PAN, "exp_month": 12, "exp_year": 2030, "cvc": CANARY_CVC},
        },
        headers={
            "Authorization": f"Bearer {credentials.api_key}",
            "Idempotency-Key": f"key-{uuid.uuid4()}",
        },
    )
    published = publish_pending(session_factory, redis_client, clock)
    consumed = consume_payments(ops_ai_sessions, redis_client, clock, "canary-consumer")
    with ops_ai_sessions() as session:
        scope = Scope(merchant_id=credentials.merchant_id)
        answer = ask(session, scope, QUESTION, InstrumentedLlm(MockLlm()))

    # Assert
    assert (signup.status_code, login.status_code, wallets.status_code, payment.status_code) == (
        201,
        201,
        200,
        201,
    )
    assert (published, consumed, answer.mode) == (1, 1, "llm")
    canaries = {
        "pan": CANARY_PAN,
        "password": CANARY_PASSWORD,
        "email": CANARY_EMAIL,
        "api_key": credentials.api_key,
        "session_token": session_token,
    }
    records = list(caplog.records)
    assert len(records) >= 1
    assert _leaks(records, canaries) == []


def test_the_leak_scan_reports_a_canary_present_in_a_message_and_in_args() -> None:
    # Arrange
    in_message = logging.LogRecord("a.logger", logging.INFO, __file__, 1, f"card {CANARY_PAN}", None, None)
    in_args = logging.LogRecord("b.logger", logging.INFO, __file__, 1, "email %s", (CANARY_EMAIL,), None)
    clean = logging.LogRecord("c.logger", logging.INFO, __file__, 1, "nothing here", None, None)
    canaries = {"pan": CANARY_PAN, "email": CANARY_EMAIL}

    # Act
    found = _leaks([in_message, in_args, clean], canaries)

    # Assert
    assert found == [
        ("pan", "a.logger", "message"),
        ("pan", "a.logger", "msg"),
        ("email", "b.logger", "message"),
        ("email", "b.logger", "args"),
    ]
