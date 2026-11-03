"""Tests for payment_facts.card_ref, ops_ai.fraud_alerts, ops_ai.tickets and their migration
(docs/specs/sq3.md section 3)."""

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

TRANSFER_FACTS_REVISION = "7c2e9a4b1d05"
FRAUD_ALERTS_REVISION = "9d4f1b7e2c63"
CREATED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")

INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES "
    "(:alert_id, :window_key, :merchant_id, :kind, :first_payment_id, :started_at, :last_seen_at, "
    ":payment_count, CAST(:features AS jsonb), :created_at)"
)
INSERT_TICKET = text(
    "INSERT INTO ops_ai.tickets (ticket_id, source_key, queue, priority, merchant_id, title, reason, "
    "alert_id, created_at) VALUES (:ticket_id, :source_key, :queue, :priority, :merchant_id, :title, "
    ":reason, :alert_id, :created_at)"
)
INSERT_TICKET_WITH_STATUS = text(
    "INSERT INTO ops_ai.tickets (ticket_id, source_key, queue, priority, status, merchant_id, title, reason, "
    "alert_id, created_at) VALUES (:ticket_id, :source_key, :queue, :priority, :status, :merchant_id, "
    ":title, :reason, :alert_id, :created_at)"
)


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _regclass_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}).scalar_one())


def _column_exists(engine: Engine, table: str, column: str) -> bool:
    query = text(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema = 'ops_ai' AND table_name = :table AND column_name = :column"
    )
    with engine.connect() as conn:
        return bool(conn.execute(query, {"table": table, "column": column}).scalar_one())


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, str, int | None]]:
    """name -> (normalised type, is_nullable, character_maximum_length); text-like types are one family."""
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = 'ops_ai' AND table_name = :table"
    )
    families = {"integer": "integer-like", "bigint": "integer-like"}
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


def _alert_params(**overrides: object) -> dict[str, object]:
    alert_id = uuid.uuid4()
    params: dict[str, object] = {
        "alert_id": alert_id,
        "window_key": f"card_testing:{MERCHANT}:{uuid.uuid4()}",
        "merchant_id": MERCHANT,
        "kind": "card_testing",
        "first_payment_id": uuid.uuid4(),
        "started_at": STARTED_AT,
        "last_seen_at": CREATED_AT,
        "payment_count": 12,
        "features": '{"micro_payments": 12}',
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _ticket_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "ticket_id": uuid.uuid4(),
        "source_key": f"card_testing:{MERCHANT}:{uuid.uuid4()}",
        "queue": "fraud",
        "priority": "high",
        "merchant_id": MERCHANT,
        "title": "Card testing suspected",
        "reason": "12 micro payments",
        "alert_id": None,
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


def _insert_ticket(session_factory: sessionmaker[Session], **overrides: object) -> None:
    with session_factory.begin() as session:
        session.execute(INSERT_TICKET, _ticket_params(**overrides))


# --- metadata and columns -------------------------------------------------------------------------------


def test_fraud_alerts_and_tickets_are_part_of_the_ops_ai_metadata() -> None:
    # Arrange / Act
    tables = set(TARGET_METADATA.tables)

    # Assert
    assert {"ops_ai.fraud_alerts", "ops_ai.tickets"} <= tables


def test_payment_facts_card_ref_is_a_nullable_varchar_32(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "payment_facts")

    # Assert
    assert columns["card_ref"] == ("character varying", "YES", 32)


def test_fraud_alerts_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "fraud_alerts")

    # Assert
    assert columns == {
        "alert_id": ("uuid", "NO", None),
        "window_key": ("text", "NO", None),
        "merchant_id": ("uuid", "NO", None),
        "kind": ("character varying", "NO", 32),
        "first_payment_id": ("uuid", "NO", None),
        "started_at": ("timestamp with time zone", "NO", None),
        "last_seen_at": ("timestamp with time zone", "NO", None),
        "payment_count": ("integer-like", "NO", None),
        "features": ("jsonb", "NO", None),
        "created_at": ("timestamp with time zone", "NO", None),
    }


def test_tickets_columns_have_the_specified_types_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "tickets")
    short_codes = {"queue", "priority", "status"}

    # Assert: the code columns are strings whose length the spec leaves open
    observed = {
        name: (
            ("string" if name in short_codes else column[0]),
            column[1],
            None if name in short_codes else column[2],
        )
        for name, column in columns.items()
    }
    assert {name: columns[name][0] for name in short_codes} == {
        "queue": "character varying",
        "priority": "character varying",
        "status": "character varying",
    }
    assert observed == {
        "ticket_id": ("uuid", "NO", None),
        "source_key": ("text", "NO", None),
        "queue": ("string", "NO", None),
        "priority": ("string", "NO", None),
        "status": ("string", "NO", None),
        "merchant_id": ("uuid", "YES", None),
        "title": ("text", "NO", None),
        "reason": ("text", "NO", None),
        "alert_id": ("uuid", "YES", None),
        "created_at": ("timestamp with time zone", "NO", None),
    }


def test_alert_id_and_ticket_id_are_the_primary_keys(engine: Engine) -> None:
    # Arrange / Act
    keys = (_primary_key(engine, "ops_ai.fraud_alerts"), _primary_key(engine, "ops_ai.tickets"))

    # Assert
    assert keys == (["alert_id"], ["ticket_id"])


def test_window_key_and_source_key_have_unique_indexes_and_merchant_id_a_btree_index(engine: Engine) -> None:
    # Arrange / Act
    alert_indexes = _index_definitions(engine, "fraud_alerts")
    ticket_indexes = _index_definitions(engine, "tickets")

    # Assert
    assert any("UNIQUE" in d and "(window_key)" in d for d in alert_indexes)
    assert any("USING btree (merchant_id)" in d for d in alert_indexes)
    assert any("UNIQUE" in d and "(source_key)" in d for d in ticket_indexes)
    assert any("USING btree (merchant_id)" in d for d in ticket_indexes)


# --- constraints -----------------------------------------------------------------------------------------


def test_a_ticket_without_a_status_is_stored_as_open(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_ticket(session_factory)

    # Act
    with session_factory() as session:
        statuses = session.execute(text("SELECT status FROM ops_ai.tickets")).scalars().all()

    # Assert
    assert statuses == ["open"]


@pytest.mark.parametrize("queue", ["fraud", "payments_ops", "settlement", "compliance"])
def test_every_specified_queue_is_accepted(session_factory: sessionmaker[Session], queue: str) -> None:
    # Arrange
    _insert_ticket(session_factory, queue=queue)

    # Act
    with session_factory() as session:
        stored = session.execute(text("SELECT queue FROM ops_ai.tickets")).scalars().all()

    # Assert
    assert stored == [queue]


@pytest.mark.parametrize("priority", ["high", "normal", "low"])
def test_every_specified_priority_is_accepted(session_factory: sessionmaker[Session], priority: str) -> None:
    # Arrange
    _insert_ticket(session_factory, priority=priority)

    # Act
    with session_factory() as session:
        stored = session.execute(text("SELECT priority FROM ops_ai.tickets")).scalars().all()

    # Assert
    assert stored == [priority]


@pytest.mark.parametrize("status", ["open", "assigned", "resolved"])
def test_every_specified_status_is_accepted(session_factory: sessionmaker[Session], status: str) -> None:
    # Arrange
    params = _ticket_params(status=status)

    # Act
    with session_factory.begin() as session:
        session.execute(INSERT_TICKET_WITH_STATUS, params)
    with session_factory() as session:
        stored = session.execute(text("SELECT status FROM ops_ai.tickets")).scalars().all()

    # Assert
    assert stored == [status]


@pytest.mark.parametrize(
    ("column", "value"),
    [("queue", "billing"), ("queue", "FRAUD"), ("priority", "urgent"), ("status", "closed")],
    ids=["unknown_queue", "queue_wrong_case", "unknown_priority", "unknown_status"],
)
def test_a_ticket_outside_the_check_lists_is_rejected_by_a_check_constraint(
    session_factory: sessionmaker[Session], column: str, value: str
) -> None:
    # Arrange
    params = _ticket_params(status="open")
    params[column] = value

    # Act
    with pytest.raises(IntegrityError) as raised, session_factory.begin() as session:
        session.execute(INSERT_TICKET_WITH_STATUS, params)

    # Assert
    assert "violates check constraint" in str(raised.value.orig)
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM ops_ai.tickets")).scalar_one() == 0


def test_a_second_ticket_with_the_same_source_key_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_ticket(session_factory, source_key="card_testing:dup")

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_ticket(session_factory, source_key="card_testing:dup")

    # Assert
    assert "unique" in str(raised.value.orig).lower()


def test_a_second_alert_with_the_same_window_key_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_alert(session_factory, window_key="card_testing:dup")

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_alert(session_factory, window_key="card_testing:dup")

    # Assert
    assert "unique" in str(raised.value.orig).lower()


def test_a_ticket_may_reference_an_existing_alert(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)

    # Act
    _insert_ticket(session_factory, alert_id=alert_id)

    # Assert
    with session_factory() as session:
        stored = session.execute(text("SELECT alert_id FROM ops_ai.tickets")).scalars().all()
    assert stored == [alert_id]


def test_a_ticket_referencing_an_unknown_alert_is_rejected_by_the_foreign_key(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    unknown_alert = uuid.uuid4()

    # Act
    with pytest.raises(IntegrityError) as raised:
        _insert_ticket(session_factory, alert_id=unknown_alert)

    # Assert
    assert "foreign key" in str(raised.value.orig).lower()


def test_a_ticket_without_merchant_or_alert_reads_back_both_as_null(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_ticket(session_factory, merchant_id=None, alert_id=None)

    # Act
    with session_factory() as session:
        row = session.execute(text("SELECT merchant_id, alert_id FROM ops_ai.tickets")).one()

    # Assert
    assert tuple(row) == (None, None)


def test_alert_features_round_trip_as_jsonb(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_alert(session_factory, features='{"distinct_cards": 4, "reasons": ["micro_burst"]}')

    # Act
    with session_factory() as session:
        features = session.execute(text("SELECT features FROM ops_ai.fraud_alerts")).scalar_one()

    # Assert
    assert features == {"distinct_cards": 4, "reasons": ["micro_burst"]}


# --- migration -------------------------------------------------------------------------------------------


def test_the_fraud_alerts_migration_is_a_single_step_after_the_transfer_facts_migration() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    revision = script.get_revision(FRAUD_ALERTS_REVISION)

    # Assert
    assert revision.down_revision == TRANSFER_FACTS_REVISION


def test_migration_downgrade_removes_the_new_tables_and_column_and_upgrade_restores_them(
    engine: Engine,
) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, TRANSFER_FACTS_REVISION)
        after_downgrade = (
            _regclass_exists(engine, "ops_ai.fraud_alerts"),
            _regclass_exists(engine, "ops_ai.tickets"),
            _column_exists(engine, "payment_facts", "card_ref"),
        )
    finally:
        command.upgrade(config, "head")
    after_upgrade = (
        _regclass_exists(engine, "ops_ai.fraud_alerts"),
        _regclass_exists(engine, "ops_ai.tickets"),
        _column_exists(engine, "payment_facts", "card_ref"),
    )

    # Assert
    assert after_downgrade == (False, False, False)
    assert after_upgrade == (True, True, True)


def test_models_and_migrations_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    command.check(config)

    # Assert: command.check raises AutogenerateDiffsDetected on drift; the tables are also live
    assert _regclass_exists(engine, "ops_ai.fraud_alerts") is True
    assert _regclass_exists(engine, "ops_ai.tickets") is True
