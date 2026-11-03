"""Tests for the ops_ai.transfer_facts table and its migration (docs/specs/sq2.md section 4)."""

import uuid
from datetime import UTC, datetime

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.schema import TARGET_METADATA
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

INCIDENTS_REVISION = "318aeacd647d"
TRANSFER_FACTS_REVISION = "7c2e9a4b1d05"
DECIDED_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)

INSERT_MINIMAL = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, decided_at, ingest_source) "
    "VALUES (:transfer_id, :sender_user_id, 180000, 'USD', 'bank_transfer', 'failed', 'US', 'IN', "
    ":decided_at, 'seed')"
)


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _table_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        found = conn.execute(text("SELECT to_regclass('ops_ai.transfer_facts') IS NOT NULL")).scalar_one()
    return bool(found)


def test_transfer_facts_table_is_part_of_the_ops_ai_metadata() -> None:
    # Arrange / Act
    tables = set(TARGET_METADATA.tables)

    # Assert
    assert "ops_ai.transfer_facts" in tables


def test_transfer_facts_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = 'ops_ai' AND table_name = 'transfer_facts'"
    )

    # Act
    with engine.connect() as conn:
        columns = {row.column_name: row for row in conn.execute(query)}

    # Assert: lengths are only specified for the short code columns; the integer width is not specified
    integer_types = {"integer", "bigint"}
    sized = {"currency", "method", "status", "country_sender", "country_receiver", "ingest_source"}
    observed = {
        name: (
            "integer-like" if row.data_type in integer_types else row.data_type,
            row.is_nullable,
            row.character_maximum_length if name in sized else None,
        )
        for name, row in columns.items()
    }
    assert observed == {
        "transfer_id": ("uuid", "NO", None),
        "sender_user_id": ("uuid", "NO", None),
        "amount_minor": ("integer-like", "NO", None),
        "currency": ("character varying", "NO", 3),
        "method": ("character varying", "NO", 16),
        "status": ("character varying", "NO", 16),
        "country_sender": ("character varying", "NO", 2),
        "country_receiver": ("character varying", "NO", 2),
        "reason_code": ("character varying", "YES", None),
        "rule_id": ("character varying", "YES", None),
        "kyc_tier": ("integer-like", "YES", None),
        "limit_minor": ("integer-like", "YES", None),
        "limit_currency": ("character varying", "YES", None),
        "next_tier_limit_minor": ("integer-like", "YES", None),
        "sender_balance_minor": ("integer-like", "YES", None),
        "decided_at": ("timestamp with time zone", "NO", None),
        "ingest_source": ("character varying", "NO", 16),
    }


def test_transfer_id_is_the_primary_key(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'ops_ai.transfer_facts'::regclass AND i.indisprimary"
    )

    # Act
    with engine.connect() as conn:
        primary_key = [row.attname for row in conn.execute(query)]

    # Assert
    assert primary_key == ["transfer_id"]


def test_transfer_facts_has_a_btree_index_on_sender_user_id(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'ops_ai' AND tablename = 'transfer_facts'"
    )

    # Act
    with engine.connect() as conn:
        definitions = [row.indexdef for row in conn.execute(query)]

    # Assert
    assert any("USING btree (sender_user_id)" in definition for definition in definitions)


def test_a_row_without_the_optional_columns_is_accepted_and_they_read_back_as_null(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = uuid.uuid4()

    # Act
    with session_factory.begin() as session:
        session.execute(
            INSERT_MINIMAL,
            {"transfer_id": transfer_id, "sender_user_id": uuid.uuid4(), "decided_at": DECIDED_AT},
        )
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT reason_code, rule_id, kyc_tier, limit_minor, limit_currency, next_tier_limit_minor, "
                "sender_balance_minor FROM ops_ai.transfer_facts"
            )
        ).one()

    # Assert
    assert tuple(row) == (None, None, None, None, None, None, None)


def test_the_transfer_facts_migration_is_a_single_step_after_the_incidents_migration() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    transfer_facts = script.get_revision(TRANSFER_FACTS_REVISION)

    # Assert
    assert transfer_facts.down_revision == INCIDENTS_REVISION


def test_migration_downgrade_removes_transfer_facts_and_upgrade_restores_it(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, INCIDENTS_REVISION)
        after_downgrade = _table_exists(engine)
    finally:
        command.upgrade(config, "head")
    after_upgrade = _table_exists(engine)

    # Assert
    assert (after_downgrade, after_upgrade) == (False, True)


def test_models_and_migrations_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    command.check(config)

    # Assert: command.check raises AutogenerateDiffsDetected on drift; the table is also live
    assert _table_exists(engine) is True
