"""Gateway-issued merchant tokens against ops-ai verification, over both HTTP apps (R1, R2, R4, R5)."""

import uuid
from dataclasses import dataclass

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.accounts.tokens import issue_merchant_token
from gateway.app import GatewayAppDeps
from gateway.app import create_app as create_gateway_app
from gateway.clock import FixedClock
from gateway.outbox.relay import publish_pending
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from ops_ai.app import OpsAiAppDeps
from ops_ai.app import create_app as create_ops_ai_app
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.auth import verify_merchant_token
from ops_ai.ingest.consumer import consume_payments
from ops_ai.scope import Scope
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINE_PAN = "4000000000000002"
QUESTION = "declined payments"


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


@dataclass(frozen=True)
class Clients:
    gateway: TestClient
    ops_ai: TestClient


def _clients(
    session_factory: sessionmaker[Session], redis_client: Redis, clock: FixedClock, keys: KeyPair
) -> Clients:
    charge_deps = ChargeDeps(
        session_factory=session_factory,
        redis=redis_client,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    gateway_app = create_gateway_app(
        GatewayAppDeps(
            charge=charge_deps, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=keys.private_pem
        )
    )
    ops_ai_app = create_ops_ai_app(
        OpsAiAppDeps(
            session_factory=session_factory, llm=MockLlm(), jwt_public_key_pem=keys.public_pem, clock=clock
        )
    )
    return Clients(gateway=TestClient(gateway_app), ops_ai=TestClient(ops_ai_app))


def _new_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[uuid.UUID, dict[str, str]]:
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    return credentials.merchant_id, {"Authorization": f"Bearer {credentials.api_key}"}


def _charge(client: TestClient, auth: dict[str, str], pan: str, key: str) -> str:
    body = {
        "amount_minor": 1500,
        "currency": "USD",
        "method": "card",
        "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }
    response = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": key})
    assert response.status_code == 201
    return str(response.json()["payment_id"])


def _access_token(client: TestClient, auth: dict[str, str]) -> str:
    response = client.post("/v1/merchant-tokens", headers=auth)
    assert response.status_code == 201
    return str(response.json()["access_token"])


def _ask(client: TestClient, token: str) -> list[str]:
    response = client.post(
        "/v1/assistant/ask", json={"question": QUESTION}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    return [str(item["ref"]) for item in response.json()["evidence"]]


def test_a_gateway_issued_token_verifies_in_ops_ai_to_the_token_merchant_scope(
    key_pair: KeyPair, clock: FixedClock
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    token = issue_merchant_token(key_pair.private_pem, merchant_id, clock())

    # Act
    scope = verify_merchant_token(token, key_pair.public_pem, clock())

    # Assert
    assert scope == Scope(merchant_id=merchant_id)


def test_a_gateway_token_issued_for_one_merchant_never_unlocks_another_merchants_evidence(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    clock: FixedClock,
    key_pair: KeyPair,
) -> None:
    # Arrange
    clients = _clients(session_factory, redis_client, clock, key_pair)
    _, auth_a = _new_merchant(session_factory, clock)
    _, auth_b = _new_merchant(session_factory, clock)
    declined_a = _charge(clients.gateway, auth_a, DECLINE_PAN, "a-declined")
    declined_b = _charge(clients.gateway, auth_b, DECLINE_PAN, "b-declined")
    publish_pending(session_factory, redis_client, clock)
    consume_payments(session_factory, redis_client, clock, "contract-consumer")
    token_a = _access_token(clients.gateway, auth_a)

    # Act
    refs = _ask(clients.ops_ai, token_a)

    # Assert
    assert f"payment:{declined_a}" in refs
    assert f"payment:{declined_b}" not in refs


def test_the_merchant_token_flow_answers_with_the_declined_payment_and_not_the_approved_one(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    clock: FixedClock,
    key_pair: KeyPair,
) -> None:
    # Arrange
    clients = _clients(session_factory, redis_client, clock, key_pair)
    _, auth = _new_merchant(session_factory, clock)
    approved = _charge(clients.gateway, auth, APPROVE_PAN, "approve-1")
    declined = _charge(clients.gateway, auth, DECLINE_PAN, "decline-1")
    publish_pending(session_factory, redis_client, clock)
    consume_payments(session_factory, redis_client, clock, "contract-consumer")
    token = _access_token(clients.gateway, auth)

    # Act
    refs = _ask(clients.ops_ai, token)

    # Assert
    assert refs == ["aggregate:decline_code:card_declined", f"payment:{declined}"]
    assert f"payment:{approved}" not in refs
