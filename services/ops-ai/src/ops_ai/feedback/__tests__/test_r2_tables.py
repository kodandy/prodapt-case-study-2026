"""Tests for the R2 tables and migration c7e3a1d9b4f2 (docs/specs/r2-agents.md section 0).

agent_messages, kyc_profiles, feedback, fraud_rule_versions and incident_feedback_scores.
"""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.schema import TARGET_METADATA
from sqlalchemy import TextClause, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

R2_REVISION = "c7e3a1d9b4f2"
PREVIOUS_REVISION = "c7d3e9a1f254"
R2_TABLES = (
    "agent_messages",
    "kyc_profiles",
    "feedback",
    "fraud_rule_versions",
    "incident_feedback_scores",
)
CREATED_AT = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
USER = uuid.UUID("eeeeeeee-0000-4000-8000-000000000005")

INSERT_MESSAGE = text(
    "INSERT INTO ops_ai.agent_messages (message_id, correlation_id, sequence, sender, recipient, intent, "
    "payload, evidence_refs, merchant_id, user_id, created_at) VALUES (:message_id, :correlation_id, "
    ":sequence, :sender, :recipient, :intent, CAST(:payload AS jsonb), CAST(:evidence_refs AS jsonb), "
    ":merchant_id, :user_id, :created_at)"
)
INSERT_KYC = text(
    "INSERT INTO ops_ai.kyc_profiles (user_id, kyc_status, country, ingest_source) VALUES "
    "(:user_id, :kyc_status, :country, :ingest_source)"
)
INSERT_FEEDBACK = text(
    "INSERT INTO ops_ai.feedback (feedback_id, merchant_id, target_kind, target_id, verdict, comment, "
    "created_at) VALUES (:feedback_id, :merchant_id, :target_kind, :target_id, :verdict, :comment, "
    ":created_at)"
)
INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES (:merchant_id, :version, "
    ":steps, :min_micro_payments, :min_micro_share, :min_distinct_cards, :reason, :feedback_id, "
    ":created_at)"
)
INSERT_SCORE_DEFAULTS = text(
    "INSERT INTO ops_ai.incident_feedback_scores (merchant_id, incident_id, updated_at) VALUES "
    "(:merchant_id, :incident_id, :updated_at)"
)


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _regclass_exists(engine: Engine, name: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": name}).scalar_one())


def _columns(engine: Engine, table: str) -> dict[str, tuple[str, str, int | None]]:
    """name -> (data_type, is_nullable, character_maximum_length)."""
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
        "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = CAST(:table AS regclass) AND i.indisprimary "
        "ORDER BY array_position(i.indkey, a.attnum)"
    )
    with engine.connect() as conn:
        return [row.attname for row in conn.execute(query, {"table": table})]


def _index_definitions(engine: Engine, table: str) -> list[str]:
    query = text("SELECT indexdef FROM pg_indexes WHERE schemaname = 'ops_ai' AND tablename = :table")
    with engine.connect() as conn:
        return [row.indexdef for row in conn.execute(query, {"table": table})]


def _message_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "message_id": uuid.uuid4(),
        "correlation_id": uuid.uuid4(),
        "sequence": 1,
        "sender": "supervisor",
        "recipient": "fraud",
        "intent": "investigate",
        "payload": '{"transfer_id": "t-1", "fraud_hold": false}',
        "evidence_refs": '["episode:abc"]',
        "merchant_id": MERCHANT,
        "user_id": None,
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _feedback_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "feedback_id": uuid.uuid4(),
        "merchant_id": MERCHANT,
        "target_kind": "alert",
        "target_id": str(uuid.uuid4()),
        "verdict": "false_positive",
        "comment": None,
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _version_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "merchant_id": MERCHANT,
        "version": 1,
        "steps": 1,
        "min_micro_payments": 12,
        "min_micro_share": 0.83,
        "min_distinct_cards": 4,
        "reason": "feedback:false_positive",
        "feedback_id": None,
        "created_at": CREATED_AT,
    }
    params.update(overrides)
    return params


def _execute(
    session_factory: sessionmaker[Session], statement: TextClause, params: dict[str, object]
) -> None:
    with session_factory.begin() as session:
        session.execute(statement, params)


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    assert table in R2_TABLES
    with session_factory() as session:
        return int(session.execute(text(f"SELECT count(*) FROM ops_ai.{table}")).scalar_one())  # noqa: S608


# --- metadata and columns -------------------------------------------------------------------------------


def test_the_five_r2_tables_are_part_of_the_ops_ai_metadata() -> None:
    # Arrange / Act
    tables = set(TARGET_METADATA.tables)

    # Assert
    assert {f"ops_ai.{name}" for name in R2_TABLES} <= tables


@pytest.mark.parametrize("table", R2_TABLES)
def test_every_r2_table_exists_after_upgrade(engine: Engine, table: str) -> None:
    # Arrange / Act
    exists = _regclass_exists(engine, f"ops_ai.{table}")

    # Assert
    assert exists is True


def test_agent_messages_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "agent_messages")

    # Assert
    assert columns == {
        "message_id": ("uuid", "NO", None),
        "correlation_id": ("uuid", "NO", None),
        "sequence": ("integer-like", "NO", None),
        "sender": ("character varying", "NO", 32),
        "recipient": ("character varying", "NO", 32),
        "intent": ("character varying", "NO", 48),
        "payload": ("jsonb", "NO", None),
        "evidence_refs": ("jsonb", "NO", None),
        "merchant_id": ("uuid", "YES", None),
        "user_id": ("uuid", "YES", None),
        "created_at": ("timestamp with time zone", "NO", None),
    }


def test_kyc_profiles_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "kyc_profiles")

    # Assert
    assert columns == {
        "user_id": ("uuid", "NO", None),
        "kyc_status": ("character varying", "NO", 16),
        "country": ("character varying", "NO", 2),
        "ingest_source": ("character varying", "NO", 16),
    }


def test_feedback_columns_have_the_specified_types_lengths_and_nullability(engine: Engine) -> None:
    # Arrange / Act
    columns = _columns(engine, "feedback")

    # Assert
    assert columns == {
        "feedback_id": ("uuid", "NO", None),
        "merchant_id": ("uuid", "NO", None),
        "target_kind": ("character varying", "NO", 16),
        "target_id": ("character varying", "NO", 128),
        "verdict": ("character varying", "NO", 16),
        "comment": ("text", "YES", None),
        "created_at": ("timestamp with time zone", "NO", None),
    }


def test_fraud_rule_versions_columns_have_the_specified_types_lengths_and_nullability(
    engine: Engine,
) -> None:
    # Arrange / Act
    columns = _columns(engine, "fraud_rule_versions")

    # Assert
    assert columns["min_micro_share"][1:] == ("NO", None)
    assert columns["min_micro_share"][0] in {"double precision", "real"}
    assert {name: column for name, column in columns.items() if name != "min_micro_share"} == {
        "merchant_id": ("uuid", "NO", None),
        "version": ("integer-like", "NO", None),
        "steps": ("integer-like", "NO", None),
        "min_micro_payments": ("integer-like", "NO", None),
        "min_distinct_cards": ("integer-like", "NO", None),
        "reason": ("character varying", "NO", 64),
        "feedback_id": ("uuid", "YES", None),
        "created_at": ("timestamp with time zone", "NO", None),
    }


def test_incident_feedback_scores_columns_have_the_specified_types_lengths_and_nullability(
    engine: Engine,
) -> None:
    # Arrange / Act
    columns = _columns(engine, "incident_feedback_scores")

    # Assert
    assert columns == {
        "merchant_id": ("uuid", "NO", None),
        "incident_id": ("character varying", "NO", 32),
        "helpful": ("integer-like", "NO", None),
        "not_helpful": ("integer-like", "NO", None),
        "updated_at": ("timestamp with time zone", "NO", None),
    }


def test_primary_keys_are_as_specified(engine: Engine) -> None:
    # Arrange / Act
    keys = {table: _primary_key(engine, f"ops_ai.{table}") for table in R2_TABLES}

    # Assert
    assert keys == {
        "agent_messages": ["message_id"],
        "kyc_profiles": ["user_id"],
        "feedback": ["feedback_id"],
        "fraud_rule_versions": ["merchant_id", "version"],
        "incident_feedback_scores": ["merchant_id", "incident_id"],
    }


def test_agent_messages_has_btree_indexes_on_correlation_id_and_merchant_id(engine: Engine) -> None:
    # Arrange / Act
    definitions = _index_definitions(engine, "agent_messages")

    # Assert
    assert any("USING btree (correlation_id)" in d for d in definitions)
    assert any("USING btree (merchant_id)" in d for d in definitions)


# --- constraints: agent_messages ------------------------------------------------------------------------


def test_an_agent_message_round_trips_its_jsonb_columns(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    params = _message_params()
    _execute(session_factory, INSERT_MESSAGE, params)

    # Act
    with session_factory() as session:
        row = session.execute(text("SELECT payload, evidence_refs, user_id FROM ops_ai.agent_messages")).one()

    # Assert
    assert row.payload == {"transfer_id": "t-1", "fraud_hold": False}
    assert row.evidence_refs == ["episode:abc"]
    assert row.user_id is None


def test_a_second_message_with_the_same_correlation_id_and_sequence_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    correlation_id = uuid.uuid4()
    _execute(session_factory, INSERT_MESSAGE, _message_params(correlation_id=correlation_id, sequence=1))

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_MESSAGE, _message_params(correlation_id=correlation_id, sequence=1))

    # Assert
    assert "unique" in str(raised.value.orig).lower()
    assert _count(session_factory, "agent_messages") == 1


def test_messages_with_the_same_sequence_in_different_correlations_and_other_sequences_are_accepted(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    correlation_id = uuid.uuid4()

    # Act
    _execute(session_factory, INSERT_MESSAGE, _message_params(correlation_id=correlation_id, sequence=1))
    _execute(session_factory, INSERT_MESSAGE, _message_params(correlation_id=correlation_id, sequence=2))
    _execute(session_factory, INSERT_MESSAGE, _message_params(sequence=1))

    # Assert
    assert _count(session_factory, "agent_messages") == 3


def test_a_message_may_carry_a_user_id_instead_of_a_merchant_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _execute(session_factory, INSERT_MESSAGE, _message_params(merchant_id=None, user_id=USER))

    # Act
    with session_factory() as session:
        row = session.execute(text("SELECT merchant_id, user_id FROM ops_ai.agent_messages")).one()

    # Assert
    assert tuple(row) == (None, USER)


# --- constraints: kyc_profiles --------------------------------------------------------------------------


@pytest.mark.parametrize("kyc_status", ["verified", "pending", "failed"])
def test_every_specified_kyc_status_is_accepted(
    session_factory: sessionmaker[Session], kyc_status: str
) -> None:
    # Arrange
    params: dict[str, object] = {
        "user_id": USER,
        "kyc_status": kyc_status,
        "country": "DE",
        "ingest_source": "seed",
    }
    _execute(session_factory, INSERT_KYC, params)

    # Act
    with session_factory() as session:
        stored = session.execute(text("SELECT kyc_status FROM ops_ai.kyc_profiles")).scalars().all()

    # Assert
    assert stored == [kyc_status]


@pytest.mark.parametrize("kyc_status", ["unknown", "VERIFIED", "approved", ""])
def test_a_kyc_status_outside_the_check_list_is_rejected(
    session_factory: sessionmaker[Session], kyc_status: str
) -> None:
    # Arrange
    params: dict[str, object] = {
        "user_id": USER,
        "kyc_status": kyc_status,
        "country": "DE",
        "ingest_source": "seed",
    }

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_KYC, params)

    # Assert
    assert "violates check constraint" in str(raised.value.orig)
    assert _count(session_factory, "kyc_profiles") == 0


def test_a_second_kyc_profile_for_the_same_user_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    params: dict[str, object] = {
        "user_id": USER,
        "kyc_status": "verified",
        "country": "DE",
        "ingest_source": "seed",
    }
    _execute(session_factory, INSERT_KYC, params)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_KYC, {**params, "kyc_status": "failed"})

    # Assert
    assert "unique" in str(raised.value.orig).lower()


# --- constraints: feedback ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target_kind", "verdict"),
    [
        ("alert", "true_positive"),
        ("alert", "false_positive"),
        ("answer", "helpful"),
        ("incident", "not_helpful"),
    ],
)
def test_specified_target_kinds_and_verdicts_are_accepted(
    session_factory: sessionmaker[Session], target_kind: str, verdict: str
) -> None:
    # Arrange
    _execute(session_factory, INSERT_FEEDBACK, _feedback_params(target_kind=target_kind, verdict=verdict))

    # Act
    with session_factory() as session:
        row = session.execute(text("SELECT target_kind, verdict, comment FROM ops_ai.feedback")).one()

    # Assert
    assert tuple(row) == (target_kind, verdict, None)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("target_kind", "ticket"),
        ("target_kind", "ALERT"),
        ("verdict", "maybe"),
        ("verdict", "True_Positive"),
    ],
    ids=["unknown_kind", "kind_wrong_case", "unknown_verdict", "verdict_wrong_case"],
)
def test_feedback_outside_the_check_lists_is_rejected(
    session_factory: sessionmaker[Session], column: str, value: str
) -> None:
    # Arrange
    params = _feedback_params()
    params[column] = value

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_FEEDBACK, params)

    # Assert
    assert "violates check constraint" in str(raised.value.orig)
    assert _count(session_factory, "feedback") == 0


def test_a_second_feedback_for_the_same_merchant_kind_and_target_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _execute(session_factory, INSERT_FEEDBACK, _feedback_params(target_id="alert-1"))

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(
            session_factory,
            INSERT_FEEDBACK,
            _feedback_params(target_id="alert-1", verdict="true_positive"),
        )

    # Assert
    assert "unique" in str(raised.value.orig).lower()
    assert _count(session_factory, "feedback") == 1


def test_the_same_target_id_is_accepted_for_another_merchant_or_another_kind(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    other_merchant = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
    _execute(session_factory, INSERT_FEEDBACK, _feedback_params(target_id="shared-1"))

    # Act
    _execute(
        session_factory,
        INSERT_FEEDBACK,
        _feedback_params(target_id="shared-1", merchant_id=other_merchant),
    )
    _execute(
        session_factory,
        INSERT_FEEDBACK,
        _feedback_params(target_id="shared-1", target_kind="answer", verdict="helpful"),
    )

    # Assert
    assert _count(session_factory, "feedback") == 3


def test_feedback_comment_round_trips(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _execute(session_factory, INSERT_FEEDBACK, _feedback_params(comment="this was a load test"))

    # Act
    with session_factory() as session:
        comment = session.execute(text("SELECT comment FROM ops_ai.feedback")).scalar_one()

    # Assert
    assert comment == "this was a load test"


# --- constraints: fraud_rule_versions -------------------------------------------------------------------


@pytest.mark.parametrize("steps", [0, 5])
def test_steps_on_the_boundaries_of_the_check_are_accepted(
    session_factory: sessionmaker[Session], steps: int
) -> None:
    # Arrange
    _execute(session_factory, INSERT_VERSION, _version_params(steps=steps))

    # Act
    with session_factory() as session:
        stored = session.execute(text("SELECT steps FROM ops_ai.fraud_rule_versions")).scalars().all()

    # Assert
    assert stored == [steps]


@pytest.mark.parametrize("steps", [-1, 6])
def test_steps_outside_zero_to_five_are_rejected(session_factory: sessionmaker[Session], steps: int) -> None:
    # Arrange
    params = _version_params(steps=steps)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_VERSION, params)

    # Assert
    assert "violates check constraint" in str(raised.value.orig)
    assert _count(session_factory, "fraud_rule_versions") == 0


def test_a_second_version_row_with_the_same_merchant_and_version_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _execute(session_factory, INSERT_VERSION, _version_params(version=1))

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_VERSION, _version_params(version=1, steps=2))

    # Assert
    assert "unique" in str(raised.value.orig).lower()


def test_the_same_version_number_is_accepted_for_another_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    other_merchant = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
    _execute(session_factory, INSERT_VERSION, _version_params(version=1))

    # Act
    _execute(session_factory, INSERT_VERSION, _version_params(version=1, merchant_id=other_merchant))

    # Assert
    assert _count(session_factory, "fraud_rule_versions") == 2


def test_a_version_row_stores_its_thresholds_and_feedback_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    feedback_id = uuid.uuid4()
    _execute(session_factory, INSERT_VERSION, _version_params(feedback_id=feedback_id))

    # Act
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT min_micro_payments, min_micro_share, min_distinct_cards, reason, feedback_id "
                "FROM ops_ai.fraud_rule_versions"
            )
        ).one()

    # Assert
    assert tuple(row) == (12, 0.83, 4, "feedback:false_positive", feedback_id)


# --- constraints: incident_feedback_scores --------------------------------------------------------------


def test_incident_feedback_counters_default_to_zero(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _execute(
        session_factory,
        INSERT_SCORE_DEFAULTS,
        {"merchant_id": MERCHANT, "incident_id": "INC-1", "updated_at": CREATED_AT},
    )

    # Act
    with session_factory() as session:
        row = session.execute(text("SELECT helpful, not_helpful FROM ops_ai.incident_feedback_scores")).one()

    # Assert
    assert tuple(row) == (0, 0)


def test_a_second_score_row_for_the_same_merchant_and_incident_is_rejected(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    params: dict[str, object] = {"merchant_id": MERCHANT, "incident_id": "INC-1", "updated_at": CREATED_AT}
    _execute(session_factory, INSERT_SCORE_DEFAULTS, params)

    # Act
    with pytest.raises(IntegrityError) as raised:
        _execute(session_factory, INSERT_SCORE_DEFAULTS, params)

    # Assert
    assert "unique" in str(raised.value.orig).lower()


# --- migration ------------------------------------------------------------------------------------------


def test_the_r2_migration_is_a_single_step_after_the_ticket_lifecycle_migration() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    revision = script.get_revision(R2_REVISION)

    # Assert
    assert revision is not None
    assert revision.down_revision == PREVIOUS_REVISION


def test_the_r2_migration_is_an_ancestor_of_the_single_alembic_head() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    heads = script.get_heads()
    ancestors = {revision.revision for revision in script.iterate_revisions(heads[0], "base")}

    # Assert
    assert len(heads) == 1
    assert R2_REVISION in ancestors


def test_migration_downgrade_removes_all_five_tables_and_upgrade_restores_them(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, PREVIOUS_REVISION)
        after_downgrade = {table: _regclass_exists(engine, f"ops_ai.{table}") for table in R2_TABLES}
    finally:
        command.upgrade(config, "head")
    after_upgrade = {table: _regclass_exists(engine, f"ops_ai.{table}") for table in R2_TABLES}

    # Assert
    assert after_downgrade == dict.fromkeys(R2_TABLES, False)
    assert after_upgrade == dict.fromkeys(R2_TABLES, True)


def test_models_and_migrations_have_no_drift(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    command.check(config)

    # Assert: command.check raises AutogenerateDiffsDetected on drift; the tables are also live
    assert _regclass_exists(engine, "ops_ai.feedback") is True
    assert _regclass_exists(engine, "ops_ai.fraud_rule_versions") is True
