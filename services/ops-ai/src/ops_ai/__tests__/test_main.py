"""Tests for ops_ai/main.py, written from docs/specs/runtime.md (R5 build_app_from_env)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.main import build_app_from_env
from sqlalchemy.orm import Session, sessionmaker

QUESTION = "declined payments"
DUMMY_DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
DUMMY_REDIS_URL = "redis://localhost:6379/0"


def _generate_pems() -> tuple[str, str]:
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


@pytest.fixture(scope="module")
def pems() -> tuple[str, str]:
    return _generate_pems()


@pytest.fixture(scope="module")
def other_pems() -> tuple[str, str]:
    return _generate_pems()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPS_AI_JWT_PUBLIC_KEY_PEM", "OPS_AI_SECRETS_DIR", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _merchant_token(private_pem: str, merchant_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _seed_declined_payment(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> uuid.UUID:
    now = datetime.now(UTC)
    payment_id = uuid.uuid4()
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=now,
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant_id,
        amount_minor=1200,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
    )
    with session_factory.begin() as session:
        apply_payment_event(session, lambda: now, event)
    return payment_id


def test_build_app_from_env_without_a_jwt_public_key_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _clear_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", DUMMY_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", DUMMY_REDIS_URL)
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))

    # Act
    with pytest.raises(RuntimeError) as exc:
        build_app_from_env()

    # Assert
    assert exc.type is RuntimeError


def test_build_app_from_env_without_a_secrets_dir_or_variable_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    _clear_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", DUMMY_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", DUMMY_REDIS_URL)

    # Act
    with pytest.raises(RuntimeError) as exc:
        build_app_from_env()

    # Assert
    assert exc.type is RuntimeError


@pytest.mark.usefixtures("redis_client")
def test_build_app_from_env_uses_the_mock_llm_when_no_openai_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    private_pem, public_pem = pems
    _clear_env(monkeypatch)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(public_pem)
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    merchant_id = uuid.uuid4()
    declined_id = _seed_declined_payment(session_factory, merchant_id)
    client = TestClient(build_app_from_env())
    headers = {"Authorization": f"Bearer {_merchant_token(private_pem, merchant_id)}"}

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=headers)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert (body["mode"], body["model"]) == ("llm", "mock")
    assert f"payment:{declined_id}" in [item["ref"] for item in body["evidence"]]


@pytest.mark.usefixtures("redis_client", "session_factory")
def test_build_app_from_env_reads_the_public_key_from_the_secrets_dir_and_rejects_other_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    other_pems: tuple[str, str],
) -> None:
    # Arrange
    _clear_env(monkeypatch)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(pems[1])
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    client = TestClient(build_app_from_env())
    forged = {"Authorization": f"Bearer {_merchant_token(other_pems[0], uuid.uuid4())}"}

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=forged)

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid token"}


@pytest.mark.usefixtures("redis_client", "session_factory")
def test_build_app_from_env_accepts_the_public_key_from_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch, pems: tuple[str, str]
) -> None:
    # Arrange
    private_pem, public_pem = pems
    _clear_env(monkeypatch)
    monkeypatch.setenv("OPS_AI_JWT_PUBLIC_KEY_PEM", public_pem)
    client = TestClient(build_app_from_env())
    headers = {"Authorization": f"Bearer {_merchant_token(private_pem, uuid.uuid4())}"}

    # Act
    response = client.post("/v1/assistant/ask", json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 200
    assert response.json()["evidence"] == []


@pytest.mark.usefixtures("redis_client", "session_factory")
def test_build_app_from_env_reports_ready_when_database_and_redis_are_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pems: tuple[str, str]
) -> None:
    # Arrange
    _clear_env(monkeypatch)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(pems[1])
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    client = TestClient(build_app_from_env())

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.usefixtures("session_factory")
def test_build_app_from_env_reports_not_ready_when_redis_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pems: tuple[str, str]
) -> None:
    # Arrange
    _clear_env(monkeypatch)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(pems[1])
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    client = TestClient(build_app_from_env())

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": ["redis"]}
