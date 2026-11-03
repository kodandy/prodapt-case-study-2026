"""Tests for the payment_facts composite index, written from docs/specs/sq3.md H-2 (migration)."""

from alembic import command
from alembic.config import Config
from ops_ai.schema import TARGET_METADATA
from sqlalchemy import text
from sqlalchemy.engine import Engine

from conftest import ALEMBIC_CONFIGS

INDEX_NAME = "ix_ops_ai_payment_facts_merchant_method_occurred_at"
TRANSFER_FACTS_REVISION = "7c2e9a4b1d05"


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _index_definitions(engine: Engine) -> dict[str, str]:
    query = text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'ops_ai' AND tablename = :table"
    )
    with engine.connect() as conn:
        return {row.indexname: row.indexdef for row in conn.execute(query, {"table": "payment_facts"})}


def test_the_migrated_database_has_the_merchant_method_occurred_at_index_on_payment_facts(
    engine: Engine,
) -> None:
    # Arrange / Act
    definitions = _index_definitions(engine)

    # Assert
    assert INDEX_NAME in definitions
    assert "USING btree (merchant_id, method, occurred_at)" in definitions[INDEX_NAME]
    assert "UNIQUE" not in definitions[INDEX_NAME]


def test_the_payment_fact_model_declares_the_same_index_in_the_same_column_order() -> None:
    # Arrange
    table = TARGET_METADATA.tables["ops_ai.payment_facts"]

    # Act
    columns_by_index = {str(index.name): [column.name for column in index.columns] for index in table.indexes}

    # Assert
    assert columns_by_index[INDEX_NAME] == ["merchant_id", "method", "occurred_at"]
    assert columns_by_index["ix_ops_ai_payment_facts_merchant_id"] == ["merchant_id"]


def test_the_model_and_the_migrated_database_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act / Assert: alembic check raises CommandError when the model and the database differ
    command.check(config)
    assert INDEX_NAME in _index_definitions(engine)


def test_migration_downgrade_drops_the_index_and_upgrade_restores_it(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, TRANSFER_FACTS_REVISION)
        after_downgrade = INDEX_NAME in _index_definitions(engine)
    finally:
        command.upgrade(config, "head")
    after_upgrade = INDEX_NAME in _index_definitions(engine)

    # Assert
    assert after_downgrade is False
    assert after_upgrade is True
