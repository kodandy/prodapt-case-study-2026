"""Shared fixtures: a migrated Postgres (both Alembic histories) and a flushed Redis per test."""

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from gateway.clock import FixedClock
from gateway.db import make_engine, make_session_factory
from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

ROOT = Path(__file__).parent
ALEMBIC_CONFIGS = (ROOT / "services/gateway/alembic.ini", ROOT / "services/ops-ai/alembic.ini")
DATA_SCHEMAS = ("gateway", "simulator", "ops_ai")
TEST_NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
TEST_API_KEY_PEPPER = "test-pepper-not-a-secret"
TEST_CARD_FINGERPRINT_KEY = "test-fingerprint-key-not-a-secret"


def _database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"]


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    os.environ["DATABASE_URL"] = _database_url()
    for ini in ALEMBIC_CONFIGS:
        config = Config(str(ini))
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    eng = make_engine(_database_url())
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) AND tablename <> 'alembic_version'"
            ),
            {"schemas": list(DATA_SCHEMAS)},
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)


@pytest.fixture
def redis_client() -> Iterator[Redis]:
    client = Redis.from_url(os.environ["REDIS_URL"], decode_responses=True)
    client.flushdb()
    yield client
    client.close()


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(TEST_NOW)
