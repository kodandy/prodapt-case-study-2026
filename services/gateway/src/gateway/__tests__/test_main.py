"""Tests for gateway/main.py, written from docs/specs/runtime.md (R3 build_app_from_env)."""

import re
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.main import build_app_from_env
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER

FILE_PEPPER = "pepper-from-the-secrets-dir-0123456789"
FILE_FINGERPRINT_KEY = "fingerprint-from-the-secrets-dir-0123456789"
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


def _write_secrets(root: Path, jwt_private_key_pem: str | None) -> None:
    (root / "gateway_api_key_pepper").write_text(FILE_PEPPER)
    (root / "gateway_card_fingerprint_key").write_text(FILE_FINGERPRINT_KEY)
    if jwt_private_key_pem is not None:
        (root / "gateway_jwt_private_key_pem").write_text(jwt_private_key_pem)


def _clear_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GATEWAY_API_KEY_PEPPER",
        "GATEWAY_CARD_FINGERPRINT_KEY",
        "GATEWAY_JWT_PRIVATE_KEY_PEM",
        "GATEWAY_SECRETS_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("secrets_dir_state", ["unset", "without_the_key_file"])
def test_build_app_from_env_without_a_jwt_private_key_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, secrets_dir_state: str
) -> None:
    # Arrange
    _clear_secret_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", DUMMY_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", DUMMY_REDIS_URL)
    if secrets_dir_state == "unset":
        monkeypatch.setenv("GATEWAY_API_KEY_PEPPER", FILE_PEPPER)
        monkeypatch.setenv("GATEWAY_CARD_FINGERPRINT_KEY", FILE_FINGERPRINT_KEY)
    else:
        _write_secrets(tmp_path, jwt_private_key_pem=None)
        monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(tmp_path))

    # Act
    with pytest.raises(RuntimeError, match=re.escape("GATEWAY_JWT_PRIVATE_KEY_PEM is required")) as exc:
        build_app_from_env()

    # Assert
    assert str(exc.value) == "GATEWAY_JWT_PRIVATE_KEY_PEM is required"


def test_build_app_from_env_reads_the_secrets_dir_and_issues_verifiable_merchant_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
    clock: FixedClock,
) -> None:
    # Arrange
    private_pem, public_pem = pems
    _clear_secret_env(monkeypatch)
    _write_secrets(tmp_path, jwt_private_key_pem=private_pem)
    monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(tmp_path))
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", FILE_PEPPER)
        session.commit()
    client = TestClient(build_app_from_env())

    # Act
    response = client.post("/v1/merchant-tokens", headers={"Authorization": f"Bearer {credentials.api_key}"})

    # Assert
    body = response.json()
    assert response.status_code == 201
    claims = jwt.decode(
        body["access_token"], public_pem, algorithms=["RS256"], audience="ops-ai", issuer="gateway"
    )
    assert claims["merchant_id"] == str(credentials.merchant_id)


def test_build_app_from_env_authenticates_api_keys_with_the_pepper_from_the_secrets_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
    clock: FixedClock,
) -> None:
    # Arrange
    _clear_secret_env(monkeypatch)
    _write_secrets(tmp_path, jwt_private_key_pem=pems[0])
    monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(tmp_path))
    with session_factory() as session:
        wrong_pepper_credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    client = TestClient(build_app_from_env())

    # Act
    response = client.post(
        "/v1/merchant-tokens", headers={"Authorization": f"Bearer {wrong_pepper_credentials.api_key}"}
    )

    # Assert
    assert response.status_code == 401


@pytest.mark.usefixtures("redis_client")
def test_build_app_from_env_serves_a_charge_for_a_merchant_authenticated_with_the_file_pepper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
    clock: FixedClock,
) -> None:
    # Arrange
    _clear_secret_env(monkeypatch)
    _write_secrets(tmp_path, jwt_private_key_pem=pems[0])
    monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(tmp_path))
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", FILE_PEPPER)
        session.commit()
    client = TestClient(build_app_from_env())
    charge = {
        "amount_minor": 1500,
        "currency": "USD",
        "method": "card",
        "card": {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }

    # Act
    response = client.post(
        "/v1/payments",
        json=charge,
        headers={"Authorization": f"Bearer {credentials.api_key}", "Idempotency-Key": "main-wiring"},
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"


@pytest.mark.usefixtures("session_factory", "redis_client")
def test_build_app_from_env_reports_ready_when_database_and_redis_are_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pems: tuple[str, str]
) -> None:
    # Arrange
    _clear_secret_env(monkeypatch)
    _write_secrets(tmp_path, jwt_private_key_pem=pems[0])
    monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(tmp_path))
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
    _clear_secret_env(monkeypatch)
    _write_secrets(tmp_path, jwt_private_key_pem=pems[0])
    monkeypatch.setenv("GATEWAY_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    client = TestClient(build_app_from_env())

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": ["redis"]}
