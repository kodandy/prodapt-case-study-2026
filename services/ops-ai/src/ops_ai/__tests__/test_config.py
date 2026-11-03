"""Tests for ops_ai/config.py, written from docs/specs/runtime.md (R5 OpsAiSettings)."""

from pathlib import Path

import pytest
from ops_ai.config import OpsAiSettings
from pydantic import SecretStr, ValidationError

DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
REDIS_URL = "redis://localhost:6379/0"
PEM_VALUE = "-----BEGIN PUBLIC KEY-----\nTEST-PUBLIC-KEY-BODY\n-----END PUBLIC KEY-----"
OPENAI_CANARY = "sk-canary-openai-key-6b1f"


def _reveal(secret: SecretStr | None) -> str | None:
    return None if secret is None else secret.get_secret_value()


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.delenv("OPS_AI_JWT_PUBLIC_KEY_PEM", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return monkeypatch


def test_ops_ai_settings_reads_database_and_redis_urls_from_the_unprefixed_variables(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange: DATABASE_URL and REDIS_URL set by the fixture

    # Act
    settings = OpsAiSettings()

    # Assert
    assert (settings.database_url, settings.redis_url) == (DATABASE_URL, REDIS_URL)


def test_ops_ai_settings_optional_values_default_to_none(base_env: pytest.MonkeyPatch) -> None:
    # Arrange: no key variables set by the fixture

    # Act
    settings = OpsAiSettings()

    # Assert
    assert settings.jwt_public_key_pem is None
    assert settings.openai_api_key is None


def test_ops_ai_settings_reads_the_public_key_from_the_prefixed_variable(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange
    base_env.setenv("OPS_AI_JWT_PUBLIC_KEY_PEM", PEM_VALUE)

    # Act
    settings = OpsAiSettings()

    # Assert
    assert settings.jwt_public_key_pem == PEM_VALUE


def test_ops_ai_settings_reads_the_public_key_from_the_secrets_dir(
    base_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(PEM_VALUE)

    # Act
    settings = OpsAiSettings(_secrets_dir=tmp_path)

    # Assert
    assert settings.jwt_public_key_pem == PEM_VALUE


def test_ops_ai_settings_reads_the_openai_key_as_a_secret_from_the_unprefixed_variable(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange
    base_env.setenv("OPENAI_API_KEY", OPENAI_CANARY)

    # Act
    settings = OpsAiSettings()

    # Assert
    assert _reveal(settings.openai_api_key) == OPENAI_CANARY
    assert OPENAI_CANARY not in repr(settings)


def test_ops_ai_settings_validation_error_does_not_echo_the_input_values(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange
    base_env.delenv("DATABASE_URL")
    base_env.setenv("OPENAI_API_KEY", OPENAI_CANARY)

    # Act
    with pytest.raises(ValidationError) as exc:
        OpsAiSettings()

    # Assert
    assert [error["loc"] for error in exc.value.errors()] == [("DATABASE_URL",)]
    assert OPENAI_CANARY not in str(exc.value)
    assert REDIS_URL not in str(exc.value)
