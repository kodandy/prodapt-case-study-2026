"""OPS_AI_EMBEDDER selects the query-side embedder for hybrid search (B6.2)."""

import pytest
from ops_ai.config import OpsAiSettings
from pydantic import ValidationError

DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
REDIS_URL = "redis://localhost:6379/0"


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.delenv("OPS_AI_JWT_PUBLIC_KEY_PEM", raising=False)
    monkeypatch.delenv("OPS_AI_EMBEDDER", raising=False)
    return monkeypatch


def test_ops_ai_settings_embedder_defaults_to_local(base_env: pytest.MonkeyPatch) -> None:
    # Arrange: OPS_AI_EMBEDDER is unset by the fixture

    # Act
    settings = OpsAiSettings()

    # Assert
    assert settings.embedder == "local"


@pytest.mark.parametrize("value", ["local", "openai"])
def test_ops_ai_settings_reads_the_embedder_from_ops_ai_embedder(
    base_env: pytest.MonkeyPatch, value: str
) -> None:
    # Arrange
    base_env.setenv("OPS_AI_EMBEDDER", value)

    # Act
    settings = OpsAiSettings()

    # Assert
    assert settings.embedder == value


@pytest.mark.parametrize("value", ["gemini", "OpenAI", "local-hash-v1"])
def test_ops_ai_settings_rejects_an_unknown_embedder(base_env: pytest.MonkeyPatch, value: str) -> None:
    # Arrange
    base_env.setenv("OPS_AI_EMBEDDER", value)

    # Act
    with pytest.raises(ValidationError) as exc:
        OpsAiSettings()

    # Assert
    assert [error["loc"] for error in exc.value.errors()] == [("embedder",)]
