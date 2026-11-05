"""Tests for ops_ai.ops_alerts, ops_ai.ops_alert_deliveries and their migration (docs/specs/failure-tools.md
section 12, Tables)."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.schema import ALL_MODEL_MODULES
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

PREVIOUS_REVISION = "b12c4e7a9f31"
OPS_ALERTS_REVISION = "c4f1a8e2b7d3"
CREATED_AT = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")

INSERT_ALERT = text(
    "INSERT INTO ops_ai.ops_alerts (alert_id, source_key, kind, severity, merchant_id, title, body, "
    "created_at) "
    "VALUES (:alert_id, :source_key, :kind, :severity, :merchant_id, :title, :body, :created_at)"
)
INSERT_DELIVERY = text(
    "INSERT INTO ops_ai.ops_alert_deliveries (delivery_id, alert_id, channel, status, created_at) "
    "VALUES (:delivery_id, :alert_id, :channel, :status, :created_at)"
)


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, str, int | None]]:
    """name -> (normalised type, is_nullable, character_maximum_length); integer widths are one family."""
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = 'ops_ai' AND table_name = :table"
    )
    families = {"integer": "integer-like", "bigint": "integer-like", "smallint": "integer-like"}
    with engine.connect() as conn:
        rows = conn.execute(query, {"table": table}).all()
    return {
        row.column_name: (
            families.get(row.data_type, row.data_type),
            row.is_nullable,
            row.character_maximum_length,
        )
        for row in rows
    }


def _primary_key(engine: Engine, table: str) -> list[str]:
    query = text(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary"
    )
    with engine.connect() as conn:
        return [row.attname for row in conn.execute(query, {"table": table})]


def _index_definitions(engine: Engine, table: str) -> list[str]:
    query = text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'ops_ai' AND tablename = :table")
    with engine.connect() as conn:
        return [row.indexdef for row in conn.execute(query, {"table": table})]


def _regclass_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}).scalar_one())


def _alert_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "alert_id": uuid.uuid4(),
        "source_key": f"card_testing:{uuid.uuid4()}",
        "kind": "card_testing",
        "severity": "high",
        "merchant_id": MERCHANT,
        "title": "Card testing suspected",
        "body": "12 micro card payments",
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _delivery_params(alert_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "delivery_id": uuid.uuid4(),
        "alert_id": alert_id,
        "channel": "redis_stream",
        "status": "pending",
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _insert_alert(session_factory: sessionmaker[Session], **overrides: object) -> uuid.UUID:
    params = _alert_params(**overrides)
    with session_factory.begin() as session:
        session.execute(INSERT_ALERT, params)
    alert_id = params["alert_id"]
    assert isinstance(alert_id, uuid.UUID)
    return alert_id


def _insert_delivery(
    session_factory: sessionmaker[Session], alert_id: uuid.UUID, **overrides: object
) -> None:
    with session_factory.begin() as session:
        session.execute(INSERT_DELIVERY, _delivery_params(alert_id, **overrides))


COUNT_ROWS = {
    "ops_alerts": text("SELECT count(*) FROM ops_ai.ops_alerts"),
    "ops_alert_deliveries": text("SELECT count(*) FROM ops_ai.ops_alert_deliveries"),
}


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    with session_factory() as session:
        return int(session.execute(COUNT_ROWS[table]).scalar_one())


# --- metadata, columns, keys, indexes --------------------------------------------------------------------


def test_the_alerts_models_are_registered_with_the_ops_ai_metadata_modules() -> None:
    # Arrange / Act
    module_names = {module.__name__ for module in ALL_MODEL_MODULES}

    # Assert
    assert "ops_ai.alerts.models" in module_names


def test_ops_alerts_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "ops_alerts")

    # Assert
    assert columns == {
        "alert_id": ("uuid", "NO", None),
        "source_key": ("text", "NO", None),
        "kind": ("character varying", "NO", 32),
        "severity": ("character varying", "NO", 16),
        "merchant_id": ("uuid", "NO", None),
        "fraud_alert_id": ("uuid", "YES", None),
        "title": ("text", "NO", None),
        "body": ("text", "NO", None),
        "created_at": ("timestamp with time zone", "NO", None),
        "acknowledged_at": ("timestamp with time zone", "YES", None),
        "acknowledged_by": ("uuid", "YES", None),
    }


def test_ops_alert_deliveries_columns_have_the_specified_types_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "ops_alert_deliveries")

    # Assert: stream_entry_id's length is left open by the spec
    assert columns["stream_entry_id"][:2] == ("character varying", "YES")
    assert {name: column for name, column in columns.items() if name != "stream_entry_id"} == {
        "delivery_id": ("uuid", "NO", None),
        "alert_id": ("uuid", "NO", None),
        "channel": ("character varying", "NO", 32),
        "status": ("character varying", "NO", 16),
        "attempts": ("integer-like", "NO", None),
        "last_error": ("character varying", "YES", 64),
        "created_at": ("timestamp with time zone", "NO", None),
        "delivered_at": ("timestamp with time zone", "YES", None),
    }


def test_alert_id_and_delivery_id_are_the_primary_keys(engine: Engine) -> None:
    # Arrange / Act
    keys = (
        _primary_key(engine, "ops_ai.ops_alerts"),
        _primary_key(engine, "ops_ai.ops_alert_deliveries"),
    )

    # Assert
    assert keys == (["alert_id"], ["delivery_id"])


def test_source_key_is_uniquely_indexed_and_merchant_id_has_a_btree_index(engine: Engine) -> None:
    # Arrange / Act
    indexes = _index_definitions(engine, "ops_alerts")

    # Assert
    assert any("UNIQUE" in d and "(source_key)" in d for d in indexes)
    assert any("USING btree (merchant_id)" in d for d in indexes)


def test_alert_id_and_channel_are_uniquely_indexed_together_on_deliveries(engine: Engine) -> None:
    # Arrange / Act
    indexes = _index_definitions(engine, "ops_alert_deliveries")

    # Assert
    assert any("UNIQUE" in d and "(alert_id, channel)" in d for d in indexes)


# --- constraints -----------------------------------------------------------------------------------------


def test_a_second_alert_with_the_same_source_key_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_alert(session_factory, source_key="card_testing:dup")

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_alert(session_factory, source_key="card_testing:dup")

    # Assert
    assert "unique" in str(raised.value.orig).lower()
    assert _count(session_factory, "ops_alerts") == 1


def test_a_second_delivery_for_the_same_alert_and_channel_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)
    _insert_delivery(session_factory, alert_id, channel="redis_stream")

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_delivery(session_factory, alert_id, channel="redis_stream")

    # Assert
    assert "unique" in str(raised.value.orig).lower()
    assert _count(session_factory, "ops_alert_deliveries") == 1


def test_a_second_delivery_for_the_same_alert_on_another_channel_is_accepted(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)
    _insert_delivery(session_factory, alert_id, channel="redis_stream")

    # Act
    _insert_delivery(session_factory, alert_id, channel="email")

    # Assert
    assert _count(session_factory, "ops_alert_deliveries") == 2


@pytest.mark.parametrize("status", ["pending", "delivered", "failed"])
def test_every_specified_delivery_status_is_accepted(
    session_factory: sessionmaker[Session], status: str
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)

    # Act
    _insert_delivery(session_factory, alert_id, status=status)

    # Assert
    with session_factory() as session:
        stored = session.execute(text("SELECT status FROM ops_ai.ops_alert_deliveries")).scalars().all()
    assert stored == [status]


def test_a_delivery_status_outside_the_check_list_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_delivery(session_factory, alert_id, status="bogus")

    # Assert
    assert "violates check constraint" in str(raised.value.orig)
    assert _count(session_factory, "ops_alert_deliveries") == 0


@pytest.mark.parametrize("severity", ["high", "medium", "low"])
def test_every_specified_severity_is_accepted(session_factory: sessionmaker[Session], severity: str) -> None:
    # Arrange / Act
    _insert_alert(session_factory, severity=severity)

    # Assert
    with session_factory() as session:
        stored = session.execute(text("SELECT severity FROM ops_ai.ops_alerts")).scalars().all()
    assert stored == [severity]


def test_a_severity_outside_the_check_list_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange / Act
    with pytest.raises(IntegrityError) as raised:
        _insert_alert(session_factory, severity="bogus")

    # Assert
    assert "violates check constraint" in str(raised.value.orig)
    assert _count(session_factory, "ops_alerts") == 0


def test_a_delivery_for_an_unknown_alert_is_rejected_by_the_foreign_key(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    unknown_alert = uuid.uuid4()

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_delivery(session_factory, unknown_alert)

    # Assert
    assert "foreign key" in str(raised.value.orig).lower()


def test_a_delivery_without_attempts_starts_at_zero_and_unset_fields_read_back_null(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)

    # Act
    _insert_delivery(session_factory, alert_id)

    # Assert
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT attempts, stream_entry_id, last_error, delivered_at FROM ops_ai.ops_alert_deliveries"
            )
        ).one()
    assert tuple(row) == (0, None, None, None)


def test_an_alert_without_fraud_alert_or_acknowledgement_reads_back_nulls(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    _insert_alert(session_factory)

    # Assert
    with session_factory() as session:
        row = session.execute(
            text("SELECT fraud_alert_id, acknowledged_at, acknowledged_by FROM ops_ai.ops_alerts")
        ).one()
    assert tuple(row) == (None, None, None)


# --- migration -------------------------------------------------------------------------------------------


def test_the_ops_alerts_migration_is_a_single_step_after_the_previous_head() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    revision = script.get_revision(OPS_ALERTS_REVISION)

    # Assert
    assert revision.down_revision == PREVIOUS_REVISION


def test_models_and_migrations_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    command.check(config)

    # Assert: command.check raises AutogenerateDiffsDetected on drift; the tables are also live
    assert _regclass_exists(engine, "ops_ai.ops_alerts") is True
    assert _regclass_exists(engine, "ops_ai.ops_alert_deliveries") is True
