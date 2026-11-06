"""R2-7 per-actor id: each rule version records the acting API key (r2-agents.md section 6.1, 11-06)."""

import uuid

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.clock import Clock
from ops_ai.feedback.thresholds import RuleActorIdentity, actor_identity_of, revert_rule, rule_actor_identity
from ops_ai.scope import Scope
from sqlalchemy import TextClause, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS, TEST_NOW

REVISION = "f3c8a1d6e9b4"
PREVIOUS_REVISION = "f3c8a2d6b915"
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
API_KEY = uuid.UUID("dddddddd-0000-4000-8000-000000000004")
KEYED_SCOPE = Scope(merchant_id=MERCHANT, role="merchant_support", api_key_id=API_KEY)
UNKEYED_SCOPE = Scope(merchant_id=MERCHANT)

INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
    "(:merchant_id, :version, 1, 12, 0.83, 4, 'feedback:false_positive', NULL, :created_at)"
)
INSERT_LEGACY_ACTOR = text(
    "INSERT INTO ops_ai.fraud_rule_version_actors (merchant_id, version, actor, actor_role, created_at) "
    "VALUES (:merchant_id, :version, :actor, 'merchant_admin', :created_at)"
)
INSERT_ACTOR_WITH_IDENTITY = text(
    "INSERT INTO ops_ai.fraud_rule_version_actors "
    "(merchant_id, version, actor, actor_role, actor_kind, actor_id, created_at) "
    "VALUES (:merchant_id, :version, :actor, 'merchant_admin', :actor_kind, :actor_id, :created_at)"
)
SELECT_IDENTITY = text(
    "SELECT actor_kind, actor_id FROM ops_ai.fraud_rule_version_actors "
    "WHERE merchant_id = :merchant_id AND version = :version"
)


def _config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _execute(
    session_factory: sessionmaker[Session], statement: TextClause, params: dict[str, object]
) -> None:
    with session_factory.begin() as session:
        session.execute(statement, params)


def _insert_version(session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT) -> None:
    _execute(
        session_factory, INSERT_VERSION, {"merchant_id": merchant_id, "version": 1, "created_at": TEST_NOW}
    )


def _identity_params(actor_kind: str | None, actor_id: uuid.UUID | None) -> dict[str, object]:
    return {
        "merchant_id": MERCHANT,
        "version": 1,
        "actor": f"merchant:{MERCHANT}",
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "created_at": TEST_NOW,
    }


def _stored_identity(session_factory: sessionmaker[Session], version: int) -> tuple[object, ...]:
    with session_factory() as session:
        row = session.execute(SELECT_IDENTITY, {"merchant_id": MERCHANT, "version": version}).one()
    return tuple(row)


def _columns(engine: Engine) -> dict[str, tuple[str, str, int | None]]:
    query = text(
        "SELECT column_name, data_type, is_nullable, character_maximum_length "
        "FROM information_schema.columns "
        "WHERE table_schema = 'ops_ai' AND table_name = 'fraud_rule_version_actors'"
    )
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return {row.column_name: (row.data_type, row.is_nullable, row.character_maximum_length) for row in rows}


# --- actor_identity_of -----------------------------------------------------------------------------------


def test_actor_identity_of_a_keyed_scope_is_the_api_key() -> None:
    # Arrange
    scope = KEYED_SCOPE

    # Act
    identity = actor_identity_of(scope)

    # Assert
    assert identity == RuleActorIdentity(actor_kind="api_key", actor_id=API_KEY)


def test_actor_identity_of_a_scope_without_a_key_is_the_merchant_principal_with_no_id() -> None:
    # Arrange
    scope = UNKEYED_SCOPE

    # Act
    identity = actor_identity_of(scope)

    # Assert
    assert identity == RuleActorIdentity(actor_kind="merchant_principal", actor_id=None)


# --- writes and reads ------------------------------------------------------------------------------------


def test_a_revert_by_a_keyed_scope_stores_the_api_key_as_the_actor(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    scope = KEYED_SCOPE

    # Act
    with session_factory.begin() as session:
        written = revert_rule(session, scope, clock, 0)

    # Assert
    assert written is not None
    assert _stored_identity(session_factory, written.version) == ("api_key", API_KEY)


def test_a_revert_by_a_scope_without_a_key_stores_the_merchant_principal_without_an_id(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    scope = UNKEYED_SCOPE

    # Act
    with session_factory.begin() as session:
        written = revert_rule(session, scope, clock, 0)

    # Assert
    assert written is not None
    assert _stored_identity(session_factory, written.version) == ("merchant_principal", None)


def test_rule_actor_identity_reads_back_the_stored_api_key(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    with session_factory.begin() as session:
        written = revert_rule(session, KEYED_SCOPE, clock, 0)
    assert written is not None

    # Act
    with session_factory() as session:
        identity = rule_actor_identity(session, UNKEYED_SCOPE, written.version)

    # Assert
    assert identity == RuleActorIdentity(actor_kind="api_key", actor_id=API_KEY)


def test_rule_actor_identity_reads_a_legacy_actor_row_as_the_merchant_principal_without_an_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory)
    _execute(
        session_factory,
        INSERT_LEGACY_ACTOR,
        {"merchant_id": MERCHANT, "version": 1, "actor": f"merchant:{MERCHANT}", "created_at": TEST_NOW},
    )

    # Act
    with session_factory() as session:
        identity = rule_actor_identity(session, UNKEYED_SCOPE, 1)

    # Assert
    assert identity == RuleActorIdentity(actor_kind="merchant_principal", actor_id=None)


def test_rule_actor_identity_is_none_for_a_version_without_an_actor_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory)

    # Act
    with session_factory() as session:
        identity = rule_actor_identity(session, UNKEYED_SCOPE, 1)

    # Assert
    assert identity is None


def test_rule_actor_identity_never_reads_another_merchants_actor(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    with session_factory.begin() as session:
        written = revert_rule(session, KEYED_SCOPE, clock, 0)
    assert written is not None

    # Act
    with session_factory() as session:
        identity = rule_actor_identity(session, Scope(merchant_id=OTHER_MERCHANT), written.version)

    # Assert
    assert identity is None


# --- table constraints -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("actor_kind", "actor_id"),
    [("api_key", None), ("merchant_principal", API_KEY), ("user", API_KEY), ("", None)],
    ids=["api_key_without_id", "merchant_principal_with_id", "unknown_kind", "empty_kind"],
)
def test_an_inconsistent_or_unknown_actor_identity_is_rejected_by_a_check(
    session_factory: sessionmaker[Session], actor_kind: str, actor_id: uuid.UUID | None
) -> None:
    # Arrange
    _insert_version(session_factory)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_ACTOR_WITH_IDENTITY, _identity_params(actor_kind, actor_id))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23514"


def test_a_null_actor_kind_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_version(session_factory)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_ACTOR_WITH_IDENTITY, _identity_params(None, None))

    # Assert
    assert getattr(raised.value.orig, "sqlstate", None) == "23502"


# --- migration -------------------------------------------------------------------------------------------


def test_the_actor_identity_migration_follows_the_previous_single_head() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_config())

    # Act
    revision = script.get_revision(REVISION)
    heads = script.get_heads()

    # Assert
    assert revision is not None
    assert revision.down_revision == PREVIOUS_REVISION
    assert heads == [REVISION]


def test_downgrade_drops_only_the_two_identity_columns_and_upgrade_restores_them(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    config = _config()
    _insert_version(session_factory)
    _execute(session_factory, INSERT_ACTOR_WITH_IDENTITY, _identity_params("api_key", API_KEY))
    original = _columns(engine)

    # Act
    try:
        command.downgrade(config, PREVIOUS_REVISION)
        downgraded = _columns(engine)
        command.upgrade(config, REVISION)
        restored = _columns(engine)
    finally:
        command.upgrade(config, "head")

    # Assert
    assert set(original) - set(downgraded) == {"actor_kind", "actor_id"}
    assert set(downgraded) - set(original) == set()
    assert (original["actor_kind"], original["actor_id"]) == (
        ("character varying", "NO", 32),
        ("uuid", "YES", None),
    )
    assert restored == original
    assert _stored_identity(session_factory, 1) == ("merchant_principal", None)
