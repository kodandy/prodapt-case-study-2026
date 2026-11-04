import os

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ops_ai.assistant.openai_llm import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_FALLBACK_MODEL,
    OPENAI_BASE_URL,
    validate_base_url,
)


class OpsAiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPS_AI_", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(validation_alias="DATABASE_URL")
    redis_url: str = Field(validation_alias="REDIS_URL")
    jwt_public_key_pem: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_chat_model: str = Field(default=DEFAULT_CHAT_MODEL, validation_alias="OPENAI_CHAT_MODEL")
    openai_fallback_model: str | None = Field(
        default=DEFAULT_FALLBACK_MODEL, validation_alias="OPENAI_FALLBACK_MODEL"
    )
    openai_base_url: str = Field(default=OPENAI_BASE_URL, validation_alias="OPENAI_BASE_URL")

    @field_validator("openai_fallback_model", mode="before")
    @classmethod
    def _empty_fallback_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("openai_base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        return validate_base_url(value)


def load_ops_ai_settings() -> OpsAiSettings:
    """Env vars, plus one file per field from `OPS_AI_SECRETS_DIR` when set (compose demo secrets volume)."""
    secrets_dir = os.environ.get("OPS_AI_SECRETS_DIR")
    if secrets_dir:
        return OpsAiSettings(_secrets_dir=secrets_dir)
    return OpsAiSettings()
