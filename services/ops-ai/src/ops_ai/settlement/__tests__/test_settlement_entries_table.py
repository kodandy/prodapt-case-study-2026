"""Tests for the ops_ai.settlement_entries table and its migration (docs/specs/sq4.md section 2)."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.schema import TARGET_METADATA
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

FRAUD_ALERTS_REVISION = "9d4f1b7e2c63"
SETTLEMENT_ENTRIES_REVISION = "5e8a3c1f7b42"
INDEX_NAME = "ix_ops_ai_settlement_entries_merchant_period"
CAPTURED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")

INSERT = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, :ingest_source)"
)


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "payment_id": uuid.uuid4(),
        "merchant_id": MERCHANT,
        "amount_minor": 123_450,
        "currency": "USD",
        "captured_at": CAPTURED_AT,
        "statement_period": "2026-10",
        "refunded": False,
        "chargeback": False,
        "ingest_source": "seed",
    }
    params.update(overrides)
    return params


def _table_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        found = conn.execute(text("SELECT to_regclass('ops_ai.settlement_entries') IS NOT NULL")).scalar_one()
    return bool(found)


# --- table shape -----------------------------------------------------------------------------------------


def test_settlement_entries_table_is_part_of_the_ops_ai_metadata() -> None:
    # Arrange / Act
    tables = set(TARGET_METADATA.tables)

    # Assert
    assert "ops_ai.settlement_entries" in tables


def test_settlement_entries_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns "
        "WHERE table_schema = 'ops_ai' AND table_name = 'settlement_entries'"
    )

    # Act
    with engine.connect() as conn:
        columns = {row.column_name: row for row in conn.execute(query)}

    # Assert
    observed = {
        name: (row.data_type, row.is_nullable, row.character_maximum_length) for name, row in columns.items()
    }
    assert observed == {
        "payment_id": ("uuid", "NO", None),
        "merchant_id": ("uuid", "NO", None),
        "amount_minor": ("bigint", "NO", None),
        "currency": ("character varying", "NO", 3),
        "captured_at": ("timestamp with time zone", "NO", None),
        "statement_period": ("character varying", "NO", 7),
        "refunded": ("boolean", "NO", None),
        "chargeback": ("boolean", "NO", None),
        "ingest_source": ("character varying", "NO", 16),
    }


def test_payment_id_is_the_primary_key(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'ops_ai.settlement_entries'::regclass AND i.indisprimary"
    )

    # Act
    with engine.connect() as conn:
        primary_key = [row.attname for row in conn.execute(query)]

    # Assert
    assert primary_key == ["payment_id"]


def test_the_merchant_period_index_is_a_btree_on_merchant_id_then_statement_period(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT indexdef FROM pg_indexes "
        "WHERE schemaname = 'ops_ai' AND tablename = 'settlement_entries' AND indexname = :name"
    )

    # Act
    with engine.connect() as conn:
        definitions = [row.indexdef for row in conn.execute(query, {"name": INDEX_NAME})]

    # Assert
    assert len(definitions) == 1
    assert "USING btree (merchant_id, statement_period)" in definitions[0]


# --- constraints and round trip --------------------------------------------------------------------------


def test_a_full_row_round_trips(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    params = _params(refunded=True, chargeback=True, currency="EUR", amount_minor=9_007_199_254_740_993)

    # Act
    with session_factory.begin() as session:
        session.execute(INSERT, params)
    with session_factory() as session:
        row = session.execute(text("SELECT * FROM ops_ai.settlement_entries")).mappings().one()

    # Assert
    assert dict(row) == params


@pytest.mark.parametrize(
    "column",
    [
        "payment_id",
        "merchant_id",
        "amount_minor",
        "currency",
        "captured_at",
        "statement_period",
        "refunded",
        "chargeback",
        "ingest_source",
    ],
)
def test_a_null_in_any_column_is_rejected(session_factory: sessionmaker[Session], column: str) -> None:
    # Arrange
    params = _params(**{column: None})

    # Act / Assert
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.execute(INSERT, params)


def test_a_second_entry_with_the_same_payment_id_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    payment_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(INSERT, _params(payment_id=payment_id))

    # Act / Assert
    with pytest.raises(IntegrityError), session_factory.begin() as session:
        session.execute(INSERT, _params(payment_id=payment_id, amount_minor=1))


# --- migration -------------------------------------------------------------------------------------------


def test_the_settlement_entries_migration_is_a_single_step_after_the_fraud_alerts_migration() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    revision = script.get_revision(SETTLEMENT_ENTRIES_REVISION)

    # Assert
    assert revision.down_revision == FRAUD_ALERTS_REVISION


def test_migration_downgrade_removes_settlement_entries_and_upgrade_restores_it(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, FRAUD_ALERTS_REVISION)
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
