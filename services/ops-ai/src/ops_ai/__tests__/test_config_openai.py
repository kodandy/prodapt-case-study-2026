"""Tests for the OpenAI model and base-url settings in ops_ai/config.py (docs/specs/llm-adapter.md A2)."""

import pytest
from ops_ai.assistant.openai_llm import DEFAULT_CHAT_MODEL, DEFAULT_FALLBACK_MODEL, OPENAI_BASE_URL
from ops_ai.config import OpsAiSettings

DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
REDIS_URL = "redis://localhost:6379/0"
OPENAI_VARIABLES = ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL", "OPENAI_FALLBACK_MODEL", "OPENAI_BASE_URL")


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.delenv("OPS_AI_JWT_PUBLIC_KEY_PEM", raising=False)
    for name in OPENAI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_ops_ai_settings_openai_model_and_base_url_default_to_the_pinned_values(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange: no OPENAI_* model variables set by the fixture

    # Act
    settings = OpsAiSettings()

    # Assert
    assert (settings.openai_chat_model, settings.openai_fallback_model, settings.openai_base_url) == (
        DEFAULT_CHAT_MODEL,
        DEFAULT_FALLBACK_MODEL,
        OPENAI_BASE_URL,
    )


def test_ops_ai_settings_reads_chat_model_fallback_model_and_base_url_from_the_unprefixed_variables(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange
    base_env.setenv("OPENAI_CHAT_MODEL", "chat-override")
    base_env.setenv("OPENAI_FALLBACK_MODEL", "fallback-override")
    base_env.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9999/v1")

    # Act
    settings = OpsAiSettings()

    # Assert
    assert (settings.openai_chat_model, settings.openai_fallback_model, settings.openai_base_url) == (
        "chat-override",
        "fallback-override",
        "http://127.0.0.1:9999/v1",
    )


def test_ops_ai_settings_an_empty_fallback_model_means_no_fallback(base_env: pytest.MonkeyPatch) -> None:
    # Arrange
    base_env.setenv("OPENAI_FALLBACK_MODEL", "")

    # Act
    settings = OpsAiSettings()

    # Assert
    assert settings.openai_fallback_model in (None, "")


def test_ops_ai_settings_ignores_the_prefixed_spelling_of_the_openai_model_variables(
    base_env: pytest.MonkeyPatch,
) -> None:
    # Arrange
    base_env.setenv("OPS_AI_OPENAI_CHAT_MODEL", "prefixed-chat")
    base_env.setenv("OPS_AI_OPENAI_BASE_URL", "http://prefixed.invalid/v1")

    # Act
    settings = OpsAiSettings()

    # Assert
    assert (settings.openai_chat_model, settings.openai_base_url) == (DEFAULT_CHAT_MODEL, OPENAI_BASE_URL)
