import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class OpsAiSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPS_AI_", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(validation_alias="DATABASE_URL")
    redis_url: str = Field(validation_alias="REDIS_URL")
    jwt_public_key_pem: str | None = None
    openai_api_key: SecretStr | None = Field(default=None, validation_alias="OPENAI_API_KEY")


def load_ops_ai_settings() -> OpsAiSettings:
    """Env vars, plus one file per field from `OPS_AI_SECRETS_DIR` when set (compose demo secrets volume)."""
    secrets_dir = os.environ.get("OPS_AI_SECRETS_DIR")
    if secrets_dir:
        return OpsAiSettings(_secrets_dir=secrets_dir)
    return OpsAiSettings()
