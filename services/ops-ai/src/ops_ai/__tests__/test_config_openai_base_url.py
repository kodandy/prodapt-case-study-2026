"""Tests for the `openai_base_url` validation in ops_ai/config.py (docs/specs/llm-adapter.md H6)."""

import pytest
from ops_ai.config import OpsAiSettings
from pydantic import ValidationError

DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
REDIS_URL = "redis://localhost:6379/0"
OPENAI_VARIABLES = ("OPENAI_API_KEY", "OPENAI_CHAT_MODEL", "OPENAI_FALLBACK_MODEL", "OPENAI_BASE_URL")
PASSWORD_MARKER = "BASEURL-PW-LEAK-MARKER-5521"
HOST_MARKER = "baseurl-host-leak-marker-8843.example"

REJECTED_URLS = [
    pytest.param("http://api.openai.com/v1", id="http-on-a-public-host"),
    pytest.param("https://user:pw@api.openai.com/v1", id="userinfo"),
    pytest.param("https://api.openai.com/v1?x=1", id="query"),
    pytest.param("https://api.openai.com/v1#f", id="fragment"),
    pytest.param("ftp://api.openai.com", id="other-scheme"),
    pytest.param("http://127.0.0.1:abc/v1", id="unparsable-port"),
    pytest.param(f"https://user:{PASSWORD_MARKER}@api.openai.com/v1", id="userinfo-with-marker"),
    pytest.param(f"http://{HOST_MARKER}/v1", id="http-on-a-marker-host"),
]
ACCEPTED_URLS = [
    pytest.param("https://api.openai.com/v1", id="https-public-host"),
    pytest.param("http://127.0.0.1:8080/v1", id="http-ipv4-loopback"),
    pytest.param("http://localhost:9/v1", id="http-localhost"),
]


@pytest.fixture
def base_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.delenv("OPS_AI_JWT_PUBLIC_KEY_PEM", raising=False)
    for name in OPENAI_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_ops_ai_settings_rejects_an_unsafe_openai_base_url_at_the_openai_base_url_field(
    base_env: pytest.MonkeyPatch, url: str
) -> None:
    # Arrange
    base_env.setenv("OPENAI_BASE_URL", url)

    # Act
    with pytest.raises(ValidationError) as exc:
        OpsAiSettings()

    # Assert
    assert [error["loc"] for error in exc.value.errors()] == [("OPENAI_BASE_URL",)]


@pytest.mark.parametrize("url", REJECTED_URLS)
def test_ops_ai_settings_validation_error_never_echoes_the_rejected_base_url(
    base_env: pytest.MonkeyPatch, url: str
) -> None:
    # Arrange
    base_env.setenv("OPENAI_BASE_URL", url)

    # Act
    with pytest.raises(ValidationError) as exc:
        OpsAiSettings()

    # Assert
    message = str(exc.value)
    assert (url in message, PASSWORD_MARKER in message, HOST_MARKER in message) == (False, False, False)


@pytest.mark.parametrize("url", ACCEPTED_URLS)
def test_ops_ai_settings_accepts_a_safe_openai_base_url_unchanged(
    base_env: pytest.MonkeyPatch, url: str
) -> None:
    # Arrange
    base_env.setenv("OPENAI_BASE_URL", url)

    # Act
    settings = OpsAiSettings()

    # Assert
    assert settings.openai_base_url == url
