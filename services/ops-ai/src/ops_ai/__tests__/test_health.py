"""Tests for ops_ai/health.py, written from docs/specs/runtime.md (R5 check_ready)."""

import socket

import pytest
from ops_ai.db import make_engine, make_session_factory
from ops_ai.health import check_ready
from redis import Redis
from sqlalchemy.orm import Session, sessionmaker

CANARY_PASSWORD = "leak-canary-db-password"


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _dead_redis() -> Redis:
    return Redis.from_url(
        f"redis://127.0.0.1:{_closed_port()}/0", socket_connect_timeout=0.3, socket_timeout=0.3
    )


def _dead_session_factory() -> sessionmaker[Session]:
    url = f"postgresql+psycopg://nobody:{CANARY_PASSWORD}@127.0.0.1:{_closed_port()}/none?connect_timeout=1"
    return make_session_factory(make_engine(url))


def test_check_ready_returns_no_failures_when_database_and_redis_are_up(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange: both backends from the shared fixtures

    # Act
    failed = check_ready(session_factory, redis_client)

    # Assert
    assert failed == []


def test_check_ready_reports_redis_when_the_redis_port_is_closed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dead_redis = _dead_redis()

    # Act
    failed = check_ready(session_factory, dead_redis)

    # Assert
    assert failed == ["redis"]


def test_check_ready_reports_database_when_the_engine_url_is_bad(redis_client: Redis) -> None:
    # Arrange
    dead_sessions = _dead_session_factory()

    # Act
    failed = check_ready(dead_sessions, redis_client)

    # Assert
    assert failed == ["database"]


def test_check_ready_reports_both_components_in_database_then_redis_order() -> None:
    # Arrange
    dead_sessions = _dead_session_factory()
    dead_redis = _dead_redis()

    # Act
    failed = check_ready(dead_sessions, dead_redis)

    # Assert
    assert failed == ["database", "redis"]


def test_check_ready_returns_component_names_only_and_never_the_connection_details() -> None:
    # Arrange
    dead_sessions = _dead_session_factory()
    dead_redis = _dead_redis()

    # Act
    failed = check_ready(dead_sessions, dead_redis)

    # Assert
    assert CANARY_PASSWORD not in repr(failed)
    assert failed == ["database", "redis"]


def test_check_ready_reports_redis_instead_of_raising_for_an_unexpected_error(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    def broken_ping(*_args: object, **_kwargs: object) -> bool:
        raise RuntimeError("unexpected client failure")

    monkeypatch.setattr(redis_client, "ping", broken_ping)

    # Act
    failed = check_ready(session_factory, redis_client)

    # Assert
    assert failed == ["redis"]
