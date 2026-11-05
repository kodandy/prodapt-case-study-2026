import logging

from fastapi import FastAPI
from redis import Redis

from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, MockLlm
from ops_ai.assistant.openai_llm import OpenAiLlm
from ops_ai.assistant.openai_tool_calling import OpenAiToolCallingLlm
from ops_ai.assistant.tool_calling import ToolCallingLlm
from ops_ai.clock import system_clock
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.health import check_ready
from ops_ai.observability.llm import InstrumentedLlm
from ops_ai.observability.tracing import configure_logging
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.openai_embedder import OpenAiEmbedder

REDIS_CONNECT_TIMEOUT_SECONDS = 1.0
REDIS_SOCKET_TIMEOUT_SECONDS = 2.0


def build_app_from_env() -> FastAPI:
    """uvicorn factory (`--factory ops_ai.main:build_app_from_env`). Refuses to start without the public key.

    Without OPENAI_API_KEY (unset or empty) the assistant uses MockLlm (evidence-led, model "mock").
    OPS_AI_EMBEDDER=openai needs OPENAI_API_KEY (RuntimeError otherwise); the default is the local embedder.
    """
    configure_logging(logging.INFO)
    settings = load_ops_ai_settings()
    if settings.jwt_public_key_pem is None:
        raise RuntimeError("OPS_AI_JWT_PUBLIC_KEY_PEM is required")
    session_factory = make_session_factory(make_engine(settings.database_url))
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    llm: LlmPort = MockLlm()
    tool_llm: ToolCallingLlm | None = None
    api_key = settings.openai_api_key
    if api_key is not None and api_key.get_secret_value():
        llm = OpenAiLlm(
            api_key=api_key,
            chat_model=settings.openai_chat_model,
            fallback_model=settings.openai_fallback_model,
            base_url=settings.openai_base_url,
        )
        tool_llm = OpenAiToolCallingLlm(
            api_key=api_key,
            chat_model=settings.openai_chat_model,
            fallback_model=settings.openai_fallback_model,
            base_url=settings.openai_base_url,
        )
    embedder: Embedder = LocalHashEmbedder()
    if settings.embedder == "openai":
        if api_key is None or not api_key.get_secret_value():
            raise RuntimeError("OPS_AI_EMBEDDER=openai requires OPENAI_API_KEY")
        embedder = OpenAiEmbedder(api_key, base_url=settings.openai_base_url)
    return create_app(
        OpsAiAppDeps(
            session_factory=session_factory,
            llm=InstrumentedLlm(llm),
            tool_llm=tool_llm,
            jwt_public_key_pem=settings.jwt_public_key_pem,
            clock=system_clock,
            readiness=lambda: check_ready(session_factory, redis),
            embedder=embedder,
            redis=redis,
            judge_llm=None if isinstance(llm, MockLlm) else InstrumentedLlm(llm),
        )
    )
