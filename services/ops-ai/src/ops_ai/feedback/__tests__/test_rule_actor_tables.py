"""Additive live-rule attribution tables and migration (r2-agents.md section 6.0)."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.feedback.models import FraudRuleVersionActor
from ops_ai.fraud_watch.models import FraudAlertRuleVersion
from ops_ai.schema import ALL_MODEL_MODULES, TARGET_METADATA
from sqlalchemy import TextClause, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

REVISION = "d2b7e4a9c1f6"
PREVIOUS_REVISION = "c4f1a8e2b7d3"
NEW_TABLES = ("fraud_rule_version_actors", "fraud_alert_rule_versions")
CREATED_AT = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
ALERT_ID = uuid.UUID("cccccccc-0000-4000-8000-000000000003")

INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
    "(:merchant_id, :version, 1, 12, 0.83, 4, 'feedback:false_positive', NULL, :created_at)"
)
INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, "
    "started_at, last_seen_at, payment_count, features, created_at) VALUES "
    "(:alert_id, :window_key, :merchant_id, 'card_testing', :first_payment_id, :created_at, "
    ":created_at, 12, CAST(:features AS jsonb), :created_at)"
)
INSERT_ACTOR = text(
    "INSERT INTO ops_ai.fraud_rule_version_actors (merchant_id, version, actor, actor_role, created_at) "
    "VALUES (:merchant_id, :version, :actor, :actor_role, :created_at)"
)
INSERT_APPLIED_VERSION = text(
    "INSERT INTO ops_ai.fraud_alert_rule_versions (alert_id, merchant_id, rule_version, applied_at) "
    "VALUES (:alert_id, :merchant_id, :rule_version, :applied_at)"
)
COUNT_ROWS = {
    "fraud_rule_version_actors": text("SELECT count(*) FROM ops_ai.fraud_rule_version_actors"),
    "fraud_alert_rule_versions": text("SELECT count(*) FROM ops_ai.fraud_alert_rule_versions"),
    "fraud_rule_versions": text("SELECT count(*) FROM ops_ai.fraud_rule_versions"),
    "fraud_alerts": text("SELECT count(*) FROM ops_ai.fraud_alerts"),
}


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _regclass_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}).scalar_one())


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, str, int | None]]:
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns WHERE table_schema = 'ops_ai' AND table_name = :table"
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"table": table}).all()
    return {row.column_name: (row.data_type, row.is_nullable, row.character_maximum_length) for row in rows}


def _primary_key(engine: Engine, table: str) -> list[str]:
    query = text(
        "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)"
    )
    with engine.connect() as conn:
        return [row.attname for row in conn.execute(query, {"table": table})]


def _foreign_keys(engine: Engine, table: str) -> list[tuple[tuple[str, ...], str, tuple[str, ...], str]]:
    query = text(
        "SELECT ARRAY(SELECT a.attname FROM pg_attribute a WHERE a.attrelid = c.conrelid "
        "AND a.attnum = ANY(c.conkey) ORDER BY array_position(c.conkey, a.attnum)) AS source_columns, "
        "n.nspname || '.' || r.relname AS target_table, "
        "ARRAY(SELECT a.attname FROM pg_attribute a WHERE a.attrelid = c.confrelid "
        "AND a.attnum = ANY(c.confkey) ORDER BY array_position(c.confkey, a.attnum)) AS target_columns, "
        "c.confdeltype FROM pg_constraint c JOIN pg_class r ON r.oid = c.confrelid "
        "JOIN pg_namespace n ON n.oid = r.relnamespace "
        "WHERE c.conrelid = CAST(:table AS regclass) AND c.contype = 'f' ORDER BY c.conname"
    )
    with engine.connect() as conn:
        rows = conn.execute(query, {"table": table}).all()
    return [
        (tuple(row.source_columns), row.target_table, tuple(row.target_columns), row.confdeltype)
        for row in rows
    ]


def _tables(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'ops_ai' ORDER BY tablename")
            ).scalars()
        )


def _actor_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "merchant_id": MERCHANT,
        "version": 1,
        "actor": f"merchant:{MERCHANT}",
        "actor_role": "merchant_admin",
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _applied_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "alert_id": ALERT_ID,
        "merchant_id": MERCHANT,
        "rule_version": 1,
        "applied_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _execute(
    session_factory: sessionmaker[Session], statement: TextClause, params: dict[str, object]
) -> None:
    with session_factory.begin() as session:
        session.execute(statement, params)


def _insert_version(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT, version: int = 1
) -> None:
    _execute(
        session_factory,
        INSERT_VERSION,
        {"merchant_id": merchant_id, "version": version, "created_at": CREATED_AT},
    )


def _insert_alert(session_factory: sessionmaker[Session], alert_id: uuid.UUID = ALERT_ID) -> None:
    _execute(
        session_factory,
        INSERT_ALERT,
        {
            "alert_id": alert_id,
            "window_key": f"card_testing:{alert_id}",
            "merchant_id": MERCHANT,
            "first_payment_id": uuid.UUID("dddddddd-0000-4000-8000-000000000004"),
            "features": '{"micro_payments": 12, "distinct_cards": 4}',
            "created_at": CREATED_AT,
        },
    )


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    with session_factory() as session:
        return int(session.execute(COUNT_ROWS[table]).scalar_one())


def _parent_data(engine: Engine) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with engine.connect() as conn:
        versions = conn.execute(
            text("SELECT * FROM ops_ai.fraud_rule_versions ORDER BY merchant_id, version")
        ).all()
        alerts = conn.execute(text("SELECT * FROM ops_ai.fraud_alerts ORDER BY alert_id")).all()
    return [tuple(row) for row in versions], [tuple(row) for row in alerts]


def test_new_models_are_registered_in_the_existing_metadata_modules() -> None:
    # Arrange
    module_names = {module.__name__ for module in ALL_MODEL_MODULES}

    # Act
    tables = tuple(TARGET_METADATA.tables[f"ops_ai.{name}"] for name in NEW_TABLES)

    # Assert
    assert {
        name for name in module_names if name in {"ops_ai.feedback.models", "ops_ai.fraud_watch.models"}
    } == {
        "ops_ai.feedback.models",
        "ops_ai.fraud_watch.models",
    }
    assert tuple(table.fullname for table in tables) == tuple(f"ops_ai.{name}" for name in NEW_TABLES)
    assert tables[0] is FraudRuleVersionActor.__table__
    assert tables[1] is FraudAlertRuleVersion.__table__


@pytest.mark.parametrize("table", NEW_TABLES)
def test_new_table_exists_after_upgrade(engine: Engine, table: str) -> None:
    # Arrange
    name = f"ops_ai.{table}"

    # Act
    exists = _regclass_exists(engine, name)

    # Assert
    assert exists is True


def test_actor_columns_have_the_exact_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange
    table = "fraud_rule_version_actors"

    # Act
    columns = _columns(engine, table)

    # Assert
    assert columns == {
        "merchant_id": ("uuid", "NO", None),
        "version": ("integer", "NO", None),
        "actor": ("character varying", "NO", 64),
        "actor_role": ("character varying", "NO", 32),
        "created_at": ("timestamp with time zone", "NO", None),
    }


def test_applied_rule_columns_have_the_exact_specified_types_and_nullability(engine: Engine) -> None:
    # Arrange
    table = "fraud_alert_rule_versions"

    # Act
    columns = _columns(engine, table)

    # Assert
    assert columns == {
        "alert_id": ("uuid", "NO", None),
        "merchant_id": ("uuid", "NO", None),
        "rule_version": ("integer", "NO", None),
        "applied_at": ("timestamp with time zone", "NO", None),
    }


def test_primary_keys_are_merchant_version_and_alert_id(engine: Engine) -> None:
    # Arrange
    table_names = tuple(f"ops_ai.{name}" for name in NEW_TABLES)

    # Act
    keys = tuple(_primary_key(engine, name) for name in table_names)

    # Assert
    assert keys == (["merchant_id", "version"], ["alert_id"])


def test_actor_foreign_key_targets_both_parent_columns_and_cascades(engine: Engine) -> None:
    # Arrange
    table = "ops_ai.fraud_rule_version_actors"

    # Act
    keys = _foreign_keys(engine, table)

    # Assert
    assert keys == [
        (("merchant_id", "version"), "ops_ai.fraud_rule_versions", ("merchant_id", "version"), "c")
    ]


def test_applied_rule_foreign_key_targets_the_alert_and_cascades(engine: Engine) -> None:
    # Arrange
    table = "ops_ai.fraud_alert_rule_versions"

    # Act
    keys = _foreign_keys(engine, table)

    # Assert
    assert keys == [(("alert_id",), "ops_ai.fraud_alerts", ("alert_id",), "c")]


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
def test_every_specified_actor_role_round_trips(session_factory: sessionmaker[Session], role: str) -> None:
    # Arrange
    _insert_version(session_factory)

    # Act
    _execute(session_factory, INSERT_ACTOR, _actor_params(actor_role=role))
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT merchant_id, version, actor, actor_role, created_at "
                "FROM ops_ai.fraud_rule_version_actors"
            )
        ).one()

    # Assert
    assert tuple(row) == (MERCHANT, 1, f"merchant:{MERCHANT}", role, CREATED_AT)


@pytest.mark.parametrize("role", ["", "user", "support", "ops_admin", "MERCHANT_ADMIN"])
def test_actor_roles_outside_the_check_list_are_rejected(
    session_factory: sessionmaker[Session], role: str
) -> None:
    # Arrange
    _insert_version(session_factory)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_ACTOR, _actor_params(actor_role=role))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23514"
    assert _count(session_factory, "fraud_rule_version_actors") == 0
    assert _count(session_factory, "fraud_rule_versions") == 1


@pytest.mark.parametrize("column", ["merchant_id", "version", "actor", "actor_role", "created_at"])
def test_every_actor_column_rejects_null(session_factory: sessionmaker[Session], column: str) -> None:
    # Arrange
    _insert_version(session_factory)
    params = _actor_params(**{column: None})

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_ACTOR, params)

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23502"
    assert _count(session_factory, "fraud_rule_version_actors") == 0


def test_a_duplicate_actor_for_the_same_merchant_and_version_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory)
    _execute(session_factory, INSERT_ACTOR, _actor_params())

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_ACTOR, _actor_params(actor_role="merchant_support"))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23505"
    assert _count(session_factory, "fraud_rule_version_actors") == 1


def test_actor_keys_allow_other_merchants_and_other_versions(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_version(session_factory)
    _insert_version(session_factory, version=2)
    _insert_version(session_factory, merchant_id=OTHER_MERCHANT)

    # Act
    _execute(session_factory, INSERT_ACTOR, _actor_params())
    _execute(session_factory, INSERT_ACTOR, _actor_params(version=2))
    _execute(
        session_factory,
        INSERT_ACTOR,
        _actor_params(merchant_id=OTHER_MERCHANT, actor=f"merchant:{OTHER_MERCHANT}"),
    )
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT merchant_id, version FROM ops_ai.fraud_rule_version_actors "
                "ORDER BY merchant_id, version"
            )
        ).all()

    # Assert
    assert [tuple(row) for row in rows] == [(MERCHANT, 1), (MERCHANT, 2), (OTHER_MERCHANT, 1)]


@pytest.mark.parametrize(
    ("merchant_id", "version"),
    [(MERCHANT, 2), (OTHER_MERCHANT, 1)],
    ids=["unknown_version", "wrong_merchant"],
)
def test_actor_foreign_key_rejects_an_unknown_merchant_version_pair(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, version: int
) -> None:
    # Arrange
    _insert_version(session_factory)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_ACTOR, _actor_params(merchant_id=merchant_id, version=version))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23503"
    assert _count(session_factory, "fraud_rule_version_actors") == 0


def test_deleting_a_version_cascades_only_its_actor(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_version(session_factory)
    _insert_version(session_factory, merchant_id=OTHER_MERCHANT)
    _execute(session_factory, INSERT_ACTOR, _actor_params())
    _execute(session_factory, INSERT_ACTOR, _actor_params(merchant_id=OTHER_MERCHANT))

    # Act
    _execute(
        session_factory,
        text("DELETE FROM ops_ai.fraud_rule_versions WHERE merchant_id = :merchant_id AND version = 1"),
        {"merchant_id": MERCHANT},
    )
    with session_factory() as session:
        rows = session.execute(
            text("SELECT merchant_id, version FROM ops_ai.fraud_rule_version_actors")
        ).all()

    # Assert
    assert [tuple(row) for row in rows] == [(OTHER_MERCHANT, 1)]
    assert _count(session_factory, "fraud_rule_versions") == 1


@pytest.mark.parametrize("rule_version", [0, 1, 2147483647])
def test_nonnegative_applied_versions_round_trip_without_a_rule_version_parent(
    session_factory: sessionmaker[Session], rule_version: int
) -> None:
    # Arrange
    _insert_alert(session_factory)

    # Act
    _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params(rule_version=rule_version))
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT alert_id, merchant_id, rule_version, applied_at FROM ops_ai.fraud_alert_rule_versions"
            )
        ).one()

    # Assert
    assert tuple(row) == (ALERT_ID, MERCHANT, rule_version, CREATED_AT)
    assert _count(session_factory, "fraud_rule_versions") == 0


@pytest.mark.parametrize("rule_version", [-1, -2147483648])
def test_negative_applied_versions_are_rejected(
    session_factory: sessionmaker[Session], rule_version: int
) -> None:
    # Arrange
    _insert_alert(session_factory)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params(rule_version=rule_version))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23514"
    assert _count(session_factory, "fraud_alert_rule_versions") == 0


@pytest.mark.parametrize("column", ["alert_id", "merchant_id", "rule_version", "applied_at"])
def test_every_applied_rule_column_rejects_null(session_factory: sessionmaker[Session], column: str) -> None:
    # Arrange
    _insert_alert(session_factory)
    params = _applied_params(**{column: None})

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_APPLIED_VERSION, params)

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23502"
    assert _count(session_factory, "fraud_alert_rule_versions") == 0


def test_a_second_applied_version_for_the_same_alert_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_alert(session_factory)
    _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params())

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params(rule_version=2))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23505"
    with session_factory() as session:
        versions = (
            session.execute(text("SELECT rule_version FROM ops_ai.fraud_alert_rule_versions")).scalars().all()
        )
    assert versions == [1]


def test_an_applied_version_for_an_unknown_alert_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    params = _applied_params()

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_APPLIED_VERSION, params)

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23503"
    assert _count(session_factory, "fraud_alert_rule_versions") == 0


def test_deleting_an_alert_cascades_only_its_applied_version(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    other_alert = uuid.UUID("eeeeeeee-0000-4000-8000-000000000005")
    _insert_alert(session_factory)
    _insert_alert(session_factory, other_alert)
    _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params())
    _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params(alert_id=other_alert))

    # Act
    _execute(
        session_factory,
        text("DELETE FROM ops_ai.fraud_alerts WHERE alert_id = :alert_id"),
        {"alert_id": ALERT_ID},
    )
    with session_factory() as session:
        rows = session.execute(
            text("SELECT alert_id, rule_version FROM ops_ai.fraud_alert_rule_versions")
        ).all()

    # Assert
    assert [tuple(row) for row in rows] == [(other_alert, 1)]
    assert _count(session_factory, "fraud_alerts") == 1


def test_the_live_rule_migration_revises_the_previous_single_head() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    revision = script.get_revision(REVISION)
    if revision is None:
        pytest.fail(f"missing migration {REVISION}")

    # Assert
    assert revision.down_revision == PREVIOUS_REVISION
    assert revision.revision == REVISION


def test_the_live_rule_migration_is_an_ancestor_of_the_single_alembic_head() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    heads = script.get_heads()
    ancestors = {revision.revision for revision in script.iterate_revisions(heads[0], "base")}

    # Assert
    assert len(heads) == 1
    assert REVISION in ancestors


def test_downgrade_and_upgrade_only_remove_and_restore_the_two_new_tables(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    config = _ops_ai_config()
    _insert_version(session_factory)
    _insert_alert(session_factory)
    _execute(session_factory, INSERT_ACTOR, _actor_params())
    _execute(session_factory, INSERT_APPLIED_VERSION, _applied_params())
    original_tables = _tables(engine)
    original_columns = {name: _columns(engine, name) for name in original_tables if name not in NEW_TABLES}
    original_data = _parent_data(engine)

    # Act
    try:
        command.downgrade(config, PREVIOUS_REVISION)
        downgraded_tables = _tables(engine)
        downgraded_columns = {name: _columns(engine, name) for name in downgraded_tables}
        downgraded_data = _parent_data(engine)
        with engine.connect() as conn:
            downgraded_revision = conn.execute(
                text("SELECT version_num FROM ops_ai.alembic_version")
            ).scalar_one()
    finally:
        command.upgrade(config, "head")
    restored_tables = _tables(engine)
    restored_columns = {name: _columns(engine, name) for name in restored_tables if name not in NEW_TABLES}
    restored_data = _parent_data(engine)

    # Assert
    assert set(original_tables) - set(downgraded_tables) == set(NEW_TABLES)
    assert set(downgraded_tables) - set(original_tables) == set()
    assert downgraded_revision == PREVIOUS_REVISION
    assert restored_tables == original_tables
    assert downgraded_columns == original_columns
    assert restored_columns == original_columns
    assert downgraded_data == original_data
    assert restored_data == original_data
    assert tuple(_count(session_factory, name) for name in NEW_TABLES) == (0, 0)


def test_models_and_migrations_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    command.check(config)

    # Assert
    assert tuple(_regclass_exists(engine, f"ops_ai.{name}") for name in NEW_TABLES) == (True, True)


def test_existing_rule_version_and_fraud_alert_column_sets_remain_unchanged(engine: Engine) -> None:
    # Arrange
    expected_versions = {
        "merchant_id",
        "version",
        "steps",
        "min_micro_payments",
        "min_micro_share",
        "min_distinct_cards",
        "reason",
        "feedback_id",
        "created_at",
    }
    expected_alerts = {
        "alert_id",
        "window_key",
        "merchant_id",
        "kind",
        "first_payment_id",
        "started_at",
        "last_seen_at",
        "payment_count",
        "features",
        "created_at",
    }

    # Act
    version_columns = set(_columns(engine, "fraud_rule_versions"))
    alert_columns = set(_columns(engine, "fraud_alerts"))

    # Assert
    assert version_columns == expected_versions
    assert alert_columns == expected_alerts
