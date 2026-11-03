"""Tests for the ops_ai.incidents table and its Alembic migration (docs/specs/sq1.md section 5)."""

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

INITIAL_REVISION = "4bfd36022245"
STARTED_AT = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
RESOLVED_AT = datetime(2026, 9, 14, 16, 40, tzinfo=UTC)

INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) "
    "VALUES (:incident_id, :merchant_id, :title, :root_cause, :fix_applied, :resolution_notes, "
    ":started_at, :resolved_at, :recurrence)"
)


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _table_exists(engine: Engine) -> bool:
    with engine.connect() as conn:
        found = conn.execute(text("SELECT to_regclass('ops_ai.incidents') IS NOT NULL")).scalar_one()
    return bool(found)


def _insert(
    session_factory: sessionmaker[Session],
    incident_id: str,
    merchant_id: uuid.UUID | None,
    **overrides: object,
) -> None:
    params: dict[str, object] = {
        "incident_id": incident_id,
        "merchant_id": merchant_id,
        "title": "Card failures concentrated on one BIN",
        "root_cause": "Issuer returned timeouts",
        "fix_applied": "Rerouted the BIN to another acquirer",
        "resolution_notes": "Verified afterwards",
        "started_at": STARTED_AT,
        "resolved_at": RESOLVED_AT,
        "recurrence": False,
    }
    params.update(overrides)
    with session_factory.begin() as session:
        session.execute(INSERT_INCIDENT, params)


def test_incidents_table_is_part_of_the_ops_ai_metadata() -> None:
    # Arrange / Act
    tables = set(TARGET_METADATA.tables)

    # Assert
    assert "ops_ai.incidents" in tables


def test_incidents_columns_have_the_specified_types_and_nullability(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length, is_generated "
        "FROM information_schema.columns WHERE table_schema = 'ops_ai' AND table_name = 'incidents'"
    )

    # Act
    with engine.connect() as conn:
        columns = {row.column_name: row for row in conn.execute(query)}

    # Assert
    observed = {
        name: (row.data_type, row.is_nullable, row.character_maximum_length, row.is_generated)
        for name, row in columns.items()
    }
    assert observed == {
        "incident_id": ("character varying", "NO", 32, "NEVER"),
        "merchant_id": ("uuid", "YES", None, "NEVER"),
        "title": ("text", "NO", None, "NEVER"),
        "root_cause": ("text", "NO", None, "NEVER"),
        "fix_applied": ("text", "NO", None, "NEVER"),
        "resolution_notes": ("text", "NO", None, "NEVER"),
        "started_at": ("timestamp with time zone", "NO", None, "NEVER"),
        "resolved_at": ("timestamp with time zone", "YES", None, "NEVER"),
        "recurrence_within_7d": ("boolean", "NO", None, "NEVER"),
        "tsv": ("tsvector", "NO", None, "ALWAYS"),
    }


def test_incident_id_is_the_primary_key(engine: Engine) -> None:
    # Arrange
    query = text(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = 'ops_ai.incidents'::regclass AND i.indisprimary"
    )

    # Act
    with engine.connect() as conn:
        primary_key = [row.attname for row in conn.execute(query)]

    # Assert
    assert primary_key == ["incident_id"]


def test_incidents_has_a_btree_index_on_merchant_id_and_a_gin_index_on_tsv(engine: Engine) -> None:
    # Arrange
    query = text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'ops_ai' AND tablename = 'incidents'")

    # Act
    with engine.connect() as conn:
        definitions = [row.indexdef for row in conn.execute(query)]

    # Assert
    assert any("USING btree (merchant_id)" in definition for definition in definitions)
    assert any("USING gin (tsv)" in definition for definition in definitions)


def test_tsv_is_generated_from_title_root_cause_and_fix_applied_but_not_resolution_notes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(
        session_factory,
        "inc_t1",
        None,
        title="Elevated declines",
        root_cause="Acquirer throttling",
        fix_applied="Raised the rate limit",
        resolution_notes="Confirmed zeppelin",
    )
    query = text(
        "SELECT tsv @@ websearch_to_tsquery('english', :q) AS hit FROM ops_ai.incidents "
        "WHERE incident_id = 'inc_t1'"
    )

    # Act
    with session_factory() as session:
        hits = {
            term: session.execute(query, {"q": term}).scalar_one()
            for term in ("declines", "throttling", "limit", "zeppelin")
        }

    # Assert
    assert hits == {"declines": True, "throttling": True, "limit": True, "zeppelin": False}


def test_merchant_id_and_resolved_at_accept_null(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert(session_factory, "inc_n1", None, resolved_at=None)

    # Act
    with session_factory() as session:
        row = session.execute(
            text("SELECT merchant_id, resolved_at, recurrence_within_7d FROM ops_ai.incidents")
        ).one()

    # Assert
    assert (row.merchant_id, row.resolved_at, row.recurrence_within_7d) == (None, None, False)


def test_the_latest_migration_is_a_single_step_after_the_initial_ops_ai_schema() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    head = script.get_revision(script.get_current_head() or "")

    # Assert
    assert head.down_revision == INITIAL_REVISION


def test_migration_downgrade_removes_incidents_and_upgrade_restores_it(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, INITIAL_REVISION)
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
