"""Tests for ops_ai.alert_notifications and its migration, written from docs/specs/sq3.md N-1."""

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

PREVIOUS_REVISION = "d2b7e4a9c1f6"
REVISION = "e5a9c3f17b28"
AT = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
INSERT_FRAUD_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :window_key, :merchant_id, "
    "'card_testing', :first_payment_id, :at, :at, 12, '{}'::jsonb, :at)"
)
INSERT_NOTIFICATION = text(
    "INSERT INTO ops_ai.alert_notifications (notification_id, fraud_alert_id, merchant_id, payload, status, "
    "created_at) VALUES (:notification_id, :fraud_alert_id, :merchant_id, '{}'::jsonb, :status, :at)"
)


def _config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _fraud_alert(session_factory: sessionmaker[Session]) -> uuid.UUID:
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_FRAUD_ALERT,
            {
                "alert_id": alert_id,
                "window_key": f"card_testing:{alert_id}",
                "merchant_id": MERCHANT,
                "first_payment_id": uuid.uuid4(),
                "at": AT,
            },
        )
    return alert_id


def _notify(
    session_factory: sessionmaker[Session], fraud_alert_id: uuid.UUID, status: str = "pending"
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_NOTIFICATION,
            {
                "notification_id": uuid.uuid4(),
                "fraud_alert_id": fraud_alert_id,
                "merchant_id": MERCHANT,
                "status": status,
                "at": AT,
            },
        )


def _count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM ops_ai.alert_notifications")).scalar_one())


def _columns(engine: Engine) -> dict[str, tuple[str, str, int | None]]:
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns "
        "WHERE table_schema = 'ops_ai' AND table_name = 'alert_notifications'"
    )
    families = {"integer": "integer-like", "bigint": "integer-like", "smallint": "integer-like"}
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return {
        r.column_name: (families.get(r.data_type, r.data_type), r.is_nullable, r.character_maximum_length)
        for r in rows
    }


def _tables(engine: Engine) -> list[str]:
    query = text("SELECT tablename FROM pg_tables WHERE schemaname = 'ops_ai' ORDER BY tablename")
    with engine.connect() as conn:
        return list(conn.execute(query).scalars())


def _index_definitions(engine: Engine) -> list[str]:
    query = text(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'ops_ai' AND tablename = 'alert_notifications'"
    )
    with engine.connect() as conn:
        return list(conn.execute(query).scalars())


def test_the_notify_models_are_registered_with_the_ops_ai_metadata_modules() -> None:
    # Arrange / Act
    module_names = {module.__name__ for module in ALL_MODEL_MODULES}

    # Assert
    assert "ops_ai.notify.models" in module_names


def test_the_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine)

    # Assert
    assert columns == {
        "notification_id": ("uuid", "NO", None),
        "fraud_alert_id": ("uuid", "NO", None),
        "merchant_id": ("uuid", "NO", None),
        "payload": ("jsonb", "NO", None),
        "status": ("character varying", "NO", 16),
        "attempts": ("integer-like", "NO", None),
        "last_error": ("character varying", "YES", 64),
        "created_at": ("timestamp with time zone", "NO", None),
        "delivered_at": ("timestamp with time zone", "YES", None),
    }


def test_the_indexes_are_the_primary_key_the_unique_alert_and_status_created_at(engine: Engine) -> None:
    # Arrange / Act
    indexes = _index_definitions(engine)

    # Assert
    assert any("UNIQUE" in d and "(notification_id)" in d for d in indexes)
    assert any("UNIQUE" in d and "(fraud_alert_id)" in d for d in indexes)
    assert any(
        "ix_alert_notifications_status_created_at" in d and "(status, created_at)" in d for d in indexes
    )


def test_a_second_notification_for_the_same_alert_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    alert_id = _fraud_alert(session_factory)
    _notify(session_factory, alert_id)

    # Act / Assert
    with pytest.raises(IntegrityError):
        _notify(session_factory, alert_id)
    assert _count(session_factory) == 1


def test_a_notification_for_an_unknown_alert_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    unknown = uuid.uuid4()

    # Act / Assert
    with pytest.raises(IntegrityError):
        _notify(session_factory, unknown)
    assert _count(session_factory) == 0


def test_an_unknown_status_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    alert_id = _fraud_alert(session_factory)

    # Act / Assert
    with pytest.raises(IntegrityError):
        _notify(session_factory, alert_id, status="sent")
    assert _count(session_factory) == 0


def test_deleting_the_fraud_alert_cascades_to_its_notification(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    alert_id = _fraud_alert(session_factory)
    _notify(session_factory, alert_id)

    # Act
    with session_factory.begin() as session:
        session.execute(text("DELETE FROM ops_ai.fraud_alerts WHERE alert_id = :a"), {"a": alert_id})

    # Assert
    assert _count(session_factory) == 0


def test_the_revision_follows_the_previous_head_and_is_the_single_head() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_config())

    # Act
    revision = script.get_revision(REVISION)
    heads = script.get_heads()

    # Assert
    assert revision is not None
    assert revision.down_revision == PREVIOUS_REVISION
    assert len(heads) == 1


def test_downgrade_removes_only_the_new_table_and_upgrade_restores_it(engine: Engine) -> None:
    # Arrange
    config = _config()
    original = _tables(engine)

    # Act
    try:
        command.downgrade(config, PREVIOUS_REVISION)
        downgraded = _tables(engine)
    finally:
        command.upgrade(config, "head")
    restored = _tables(engine)

    # Assert
    assert set(original) - set(downgraded) == {"alert_notifications"}
    assert set(downgraded) - set(original) == set()
    assert restored == original


def test_models_and_migrations_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _config()

    # Act
    command.check(config)

    # Assert
    assert "alert_notifications" in _tables(engine)
