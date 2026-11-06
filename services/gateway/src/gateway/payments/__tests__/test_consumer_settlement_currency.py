"""POST /v1/me/payments carries `settlement_currency` to the charge domain (B2.3, Decision 6 of
docs/specs/gateway-basic.md): a cross-currency consumer payment settles the merchant in the requested
currency, an absent one keeps the single-currency journal, an unsupported one is a 422 and an FX failure
never reaches the provider.

Needs the 12 migration-seeded demo rates (`gateway.fx_rates`) to survive the per-test table reset.
"""

import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
PASSWORD = "consumer-fx-password-7Q"
CHARGE_RESPONSE_KEYS = ["amount_minor", "card", "currency", "decline_code", "method", "payment_id", "state"]


@dataclass(frozen=True)
class Env:
    client: TestClient
    simulator: SimulatorProvider
    session_factory: sessionmaker[Session]
    merchant_id: uuid.UUID
    user_auth: dict[str, str]


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture
def env(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> Env:
    simulator = SimulatorProvider(session_factory, clock)
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=simulator,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        merchant = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        user = sign_up(session, clock, "payer-fx@example.com", PASSWORD)
        session.commit()
    app = create_app(
        GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=private_pem)
    )
    token = issue_session_token(private_pem, user.user_id, clock(), ("pwd",))
    return Env(
        TestClient(app),
        simulator,
        session_factory,
        merchant.merchant_id,
        {"Authorization": f"Bearer {token}"},
    )


def _pay(env: Env, amount_minor: int, currency: str, settlement_currency: str | None) -> Response:
    body: dict[str, object] = {
        "merchant_id": str(env.merchant_id),
        "amount_minor": amount_minor,
        "currency": currency,
        "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }
    if settlement_currency is not None:
        body["settlement_currency"] = settlement_currency
    headers = {**env.user_auth, "Idempotency-Key": uuid.uuid4().hex}
    response: Response = env.client.post("/v1/me/payments", json=body, headers=headers)
    return response


def _merchant_payable(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT p.currency, p.amount_minor, j.fx_rate_id FROM gateway.postings p "
                "JOIN gateway.journal_entries j ON j.id = p.journal_id "
                "JOIN gateway.ledger_accounts a ON a.id = p.account_id WHERE a.kind = 'merchant_payable'"
            )
        ).mappings()
        return [dict(row) for row in rows]


def _payment_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.payments")).scalar_one())


def test_consumer_payment_with_a_settlement_currency_settles_the_merchant_in_that_currency(env: Env) -> None:
    # Arrange: 1501 USD minor at the demo USD/EUR rate floors to 1380 EUR minor (test_fx_charge.py)

    # Act
    response = _pay(env, 1_501, "USD", "EUR")

    # Assert
    assert response.status_code == 201
    assert _merchant_payable(env.session_factory) == [
        {"currency": "EUR", "amount_minor": -1_380, "fx_rate_id": "demo-USDEUR-v1"}
    ]


def test_cross_currency_consumer_payment_keeps_the_charge_response_body(env: Env) -> None:
    # Arrange

    # Act
    response = _pay(env, 1_501, "USD", "EUR")

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert sorted(body) == CHARGE_RESPONSE_KEYS
    assert (body["state"], body["amount_minor"], body["currency"]) == ("succeeded", 1_501, "USD")


def test_consumer_payment_without_a_settlement_currency_settles_in_the_card_currency(env: Env) -> None:
    # Arrange

    # Act
    response = _pay(env, 1_501, "USD", None)

    # Assert
    assert response.status_code == 201
    assert _merchant_payable(env.session_factory) == [
        {"currency": "USD", "amount_minor": -1_501, "fx_rate_id": None}
    ]


@pytest.mark.parametrize("settlement_currency", ["GBP", "usd", ""])
def test_consumer_payment_with_an_unsupported_settlement_currency_answers_422(
    env: Env, settlement_currency: str
) -> None:
    # Arrange

    # Act
    response = _pay(env, 1_501, "USD", settlement_currency)

    # Assert
    assert response.status_code == 422
    assert _payment_count(env.session_factory) == 0
    assert env.simulator.charge_count() == 0


def test_consumer_payment_whose_conversion_fails_answers_422_without_calling_the_provider(env: Env) -> None:
    # Arrange: 1 JPY converts to 0 USD minor units

    # Act
    response = _pay(env, 1, "JPY", "USD")

    # Assert
    assert (response.status_code, response.json()) == (422, {"error": "fx_amount_too_small"})
    assert _payment_count(env.session_factory) == 0
    assert env.simulator.charge_count() == 0
