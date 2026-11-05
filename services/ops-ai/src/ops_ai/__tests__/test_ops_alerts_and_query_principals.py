"""Tests for two behaviours of the real ops-ai app built by `create_app`:

1. /v1/ops/alerts is mounted behind the ops-role dependency (ops tokens only), as the compliance router is.
2. POST /v1/assistant/query answers an ops principal with 403 and still answers merchant and user principals.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.alerts import raise_ops_alert
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.scope import OPS_ROLES
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ALERTS = "/v1/ops/alerts"
QUERY = "/v1/assistant/query"
QUESTION = "Why are card payments failing this afternoon?"
FORBIDDEN_BODY = {"detail": "forbidden"}
INVALID_TOKEN_BODY = {"detail": "invalid token"}
OPS_USER = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c91")
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
ALERT_KEYS = {
    "alert_id",
    "kind",
    "severity",
    "merchant_id",
    "fraud_alert_id",
    "title",
    "body",
    "created_at",
    "acknowledged_at",
    "acknowledged_by",
    "delivery_status",
}


class FixedClock:
    """Mirrors the shared `clock` fixture; ops_ai must not import gateway (test_import_rules)."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now


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


def _token(key_pair: KeyPair, subject: str, extra: dict[str, object], roles: list[str]) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }
    return jwt.encode(claims, key_pair.private_pem, algorithm="RS256")


def _ops_token(key_pair: KeyPair, role: str) -> str:
    return _token(key_pair, f"ops:{OPS_USER}", {"ops_user_id": str(OPS_USER)}, [role])


def _merchant_token(key_pair: KeyPair) -> str:
    return _token(key_pair, f"merchant:{MERCHANT}", {"merchant_id": str(MERCHANT)}, ["merchant_admin"])


def _user_token(key_pair: KeyPair) -> str:
    return _token(key_pair, f"user:{USER}", {"user_id": str(USER)}, ["consumer"])


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _client(session_factory: sessionmaker[Session], key_pair: KeyPair) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=MockLlm(),
        jwt_public_key_pem=key_pair.public_pem,
        clock=FixedClock(TEST_NOW),
    )
    return TestClient(create_app(deps))


def _seed_alert(session_factory: sessionmaker[Session], key: str) -> uuid.UUID:
    with session_factory.begin() as session:
        alert_id = raise_ops_alert(
            session,
            FixedClock(TEST_NOW),
            source_key=f"card_testing:{key}",
            kind="card_testing",
            severity="high",
            merchant_id=MERCHANT,
            fraud_alert_id=None,
            title=f"Card testing suspected {key}",
            body=f"body {key}",
        )
    assert alert_id is not None
    return alert_id


# --- /v1/ops/alerts behind the ops-role dependency ---------------------------------------------------------


@pytest.mark.parametrize("role", OPS_ROLES)
def test_list_alerts_answers_an_ops_token_with_200_an_alerts_list_and_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ALERTS, headers=_bearer(_ops_token(key_pair, role)))

    # Assert
    assert response.status_code == 200
    assert response.json() == {"alerts": []}
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize(
    "make_token",
    [_merchant_token, _user_token],
    ids=["merchant", "user"],
)
def test_list_alerts_answers_a_non_ops_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, make_token: Callable[[KeyPair], str]
) -> None:
    # Arrange
    _seed_alert(session_factory, "a")
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ALERTS, headers=_bearer(make_token(key_pair)))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_list_alerts_answers_a_request_without_authorization_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ALERTS)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


def test_list_alerts_shows_a_raised_alert_to_an_ops_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    alert_id = _seed_alert(session_factory, "a")
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ALERTS, headers=_bearer(_ops_token(key_pair, "ops_agent")))

    # Assert
    assert response.status_code == 200
    items = response.json()["alerts"]
    assert len(items) == 1
    assert set(items[0]) == ALERT_KEYS
    assert items[0]["alert_id"] == str(alert_id)
    assert items[0]["kind"] == "card_testing"
    assert items[0]["severity"] == "high"
    assert items[0]["merchant_id"] == str(MERCHANT)
    assert items[0]["title"] == "Card testing suspected a"
    assert items[0]["acknowledged_at"] is None
    assert items[0]["acknowledged_by"] is None
    assert items[0]["delivery_status"] == "pending"


def test_acknowledge_records_the_ops_user_for_an_ops_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    alert_id = _seed_alert(session_factory, "a")
    client = _client(session_factory, key_pair)
    headers = _bearer(_ops_token(key_pair, "ops_lead"))

    # Act
    response = client.post(f"{ALERTS}/{alert_id}/ack", headers=headers)
    open_after = client.get(ALERTS, headers=headers)

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["alert_id"] == str(alert_id)
    assert response.json()["acknowledged_by"] == str(OPS_USER)
    assert response.json()["acknowledged_at"] is not None
    assert open_after.json() == {"alerts": []}


@pytest.mark.parametrize(
    "make_token",
    [_merchant_token, _user_token],
    ids=["merchant", "user"],
)
def test_acknowledge_answers_a_non_ops_token_with_403_and_leaves_the_alert_open(
    session_factory: sessionmaker[Session], key_pair: KeyPair, make_token: Callable[[KeyPair], str]
) -> None:
    # Arrange
    alert_id = _seed_alert(session_factory, "a")
    client = _client(session_factory, key_pair)

    # Act
    response = client.post(f"{ALERTS}/{alert_id}/ack", headers=_bearer(make_token(key_pair)))
    still_open = client.get(ALERTS, headers=_bearer(_ops_token(key_pair, "ops_agent")))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert [item["alert_id"] for item in still_open.json()["alerts"]] == [str(alert_id)]


# --- POST /v1/assistant/query principals -------------------------------------------------------------------


@pytest.mark.parametrize("role", OPS_ROLES)
def test_query_answers_an_ops_principal_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.post(QUERY, json={"question": QUESTION}, headers=_bearer(_ops_token(key_pair, role)))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


def test_query_still_answers_a_merchant_principal_with_200(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.post(QUERY, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair)))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_query_still_answers_a_user_principal_with_200(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.post(QUERY, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair)))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
