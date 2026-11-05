from fastapi import FastAPI
from pydantic import SecretStr
from redis import Redis

from gateway.app import GatewayAppDeps, create_app
from gateway.audit.db_role import use_db_role
from gateway.clock import system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from gateway.health import check_ready
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider

REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0


def build_app_from_env() -> FastAPI:
    """uvicorn factory (`--factory gateway.main:build_app_from_env`). Refuses to start without the JWT key."""
    settings = load_gateway_settings()
    if settings.jwt_private_key_pem is None:
        raise RuntimeError("GATEWAY_JWT_PRIVATE_KEY_PEM is required")
    engine = make_engine(settings.database_url)
    if settings.db_role:
        use_db_role(engine, settings.db_role)
    session_factory = make_session_factory(engine)
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=redis,
        provider=SimulatorProvider(session_factory, system_clock),
        clock=system_clock,
        card_fingerprint_key=settings.card_fingerprint_key.get_secret_value(),
    )
    network_token = _secret_or_none(settings.network_token)
    ops_admin_token = _secret_or_none(settings.ops_admin_token)
    if network_token is not None and network_token == ops_admin_token:
        raise RuntimeError("GATEWAY_NETWORK_TOKEN and GATEWAY_OPS_ADMIN_TOKEN must differ")
    return create_app(
        GatewayAppDeps(
            charge=charge,
            api_key_pepper=settings.api_key_pepper.get_secret_value(),
            jwt_private_key_pem=settings.jwt_private_key_pem.get_secret_value(),
            totp_encryption_key=(
                None
                if settings.totp_encryption_key is None
                else settings.totp_encryption_key.get_secret_value()
            ),
            readiness=lambda: check_ready(session_factory, redis),
            network_token=network_token,
            ops_admin_token=ops_admin_token,
            demo_top_up_enabled=settings.demo_top_up_enabled,
        )
    )


def _secret_or_none(value: SecretStr | None) -> str | None:
    return None if value is None else value.get_secret_value()
