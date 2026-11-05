import logging
import os

from fastapi import FastAPI
from redis import Redis

from gateway.app import GatewayAppDeps, create_app
from gateway.clock import system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from gateway.health import check_ready
from gateway.observability.tracing import configure_logging
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.resilience.circuit_breaker import CircuitBreaker, ResilientProvider
from gateway.resilience.rate_limit import load_rate_limit_config

REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 5.0


def build_app_from_env() -> FastAPI:
    """uvicorn factory (`--factory gateway.main:build_app_from_env`). Refuses to start without the JWT key."""
    configure_logging(logging.INFO)
    settings = load_gateway_settings()
    if settings.jwt_private_key_pem is None:
        raise RuntimeError("GATEWAY_JWT_PRIVATE_KEY_PEM is required")
    session_factory = make_session_factory(make_engine(settings.database_url))
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    simulator = SimulatorProvider(session_factory, system_clock)
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=redis,
        provider=ResilientProvider(
            simulator,
            CircuitBreaker(simulator.name),
            float(os.environ.get("GATEWAY_PROVIDER_TIMEOUT_SECONDS", DEFAULT_PROVIDER_TIMEOUT_SECONDS)),
        ),
        clock=system_clock,
        card_fingerprint_key=settings.card_fingerprint_key.get_secret_value(),
    )
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
            rate_limits=load_rate_limit_config(),
        )
    )
