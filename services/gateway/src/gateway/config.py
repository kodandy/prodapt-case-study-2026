import os

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_SECRET_LENGTH = 32


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", extra="ignore", hide_input_in_errors=True)

    database_url: str = Field(validation_alias="DATABASE_URL")
    redis_url: str = Field(validation_alias="REDIS_URL")
    api_key_pepper: SecretStr = Field(min_length=MIN_SECRET_LENGTH)
    card_fingerprint_key: SecretStr = Field(min_length=MIN_SECRET_LENGTH)
    jwt_private_key_pem: SecretStr | None = None


def load_gateway_settings() -> GatewaySettings:
    """Env vars, plus one file per field from `GATEWAY_SECRETS_DIR` when set (compose demo secrets volume)."""
    secrets_dir = os.environ.get("GATEWAY_SECRETS_DIR")
    if secrets_dir:
        return GatewaySettings(_secrets_dir=secrets_dir)
    return GatewaySettings()
