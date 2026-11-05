"""Tests for the append-only audit log, from docs/specs/compliance-rbac.md section 4 (P0-10)."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg.errors
import pytest
from gateway.accounts.service import sign_up
from gateway.audit.db_role import use_db_role
from gateway.audit.service import record_audit
from gateway.clock import FixedClock
from gateway.db import make_engine
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

APP_ROLE = "gateway_app"
OVERLONG_ACTION = "x" * 65
GATEWAY_USAGE = "SELECT has_schema_privilege(CAST(:r AS name), 'gateway', 'USAGE')"
SIMULATOR_USAGE = "SELECT has_schema_privilege(CAST(:r AS name), 'simulator', 'USAGE')"
INSERT_AUDIT = text(
    "INSERT INTO gateway.audit_log (actor_id, actor_role, action, target, summary, correlation_id, "
    "occurred_at) VALUES ('user:seed', 'consumer', 'seed.action', 'seed:target', '{}', 'corr-1', now())"
)


def _scalar(session_factory: sessionmaker[Session], query: str, **params: object) -> object:
    with session_factory() as session:
        return session.execute(text(query), params).scalar_one()


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    query = f"SELECT count(*) FROM {table}"  # noqa: S608 - callers pass fixed table names
    return int(str(_scalar(session_factory, query)))


def _seed_audit_row(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(INSERT_AUDIT)


@contextmanager
def _as_app_role(engine: Engine) -> Iterator[Connection]:
    """A connection in a transaction acting as `gateway_app`; always rolled back."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text(f"SET LOCAL ROLE {APP_ROLE}"))
            yield conn
        finally:
            transaction.rollback()


def _signed_up(session_factory: sessionmaker[Session], clock: FixedClock) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, "alice@example.com", "audit-test-password-7Q")
        session.commit()
    return result.user_id


# --- (a) the trigger rejects UPDATE and DELETE for the owner ------------------------------------------------


@pytest.mark.parametrize(
    "statement",
    ["UPDATE gateway.audit_log SET action = 'tampered'", "DELETE FROM gateway.audit_log"],
    ids=["update", "delete"],
)
def test_the_owner_cannot_update_or_delete_audit_rows(
    engine: Engine, session_factory: sessionmaker[Session], statement: str
) -> None:
    # Arrange
    _seed_audit_row(engine)

    # Act
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(text(statement))

    # Assert
    assert _scalar(session_factory, "SELECT action FROM gateway.audit_log") == "seed.action"


def test_the_owner_can_still_insert_audit_rows(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Arrange / Act
    _seed_audit_row(engine)

    # Assert
    assert _count(session_factory, "gateway.audit_log") == 1


# --- (b) the gateway_app role -----------------------------------------------------------------------------


def test_gateway_app_can_insert_and_select_audit_rows(
    engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Arrange / Act
    with _as_app_role(engine) as conn:
        conn.execute(INSERT_AUDIT)
        actions = conn.execute(text("SELECT action FROM gateway.audit_log")).scalars().all()

    # Assert
    assert actions == ["seed.action"]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE gateway.audit_log SET action = 'tampered'",
        "DELETE FROM gateway.audit_log",
        "TRUNCATE gateway.audit_log",
    ],
    ids=["update", "delete", "truncate"],
)
def test_gateway_app_is_denied_update_delete_and_truncate_on_audit_log(
    engine: Engine, session_factory: sessionmaker[Session], statement: str
) -> None:
    # Arrange
    _seed_audit_row(engine)

    # Act
    with pytest.raises(DBAPIError) as denied, _as_app_role(engine) as conn:
        conn.execute(text(statement))

    # Assert
    assert isinstance(denied.value.orig, psycopg.errors.InsufficientPrivilege)
    assert _scalar(session_factory, "SELECT action FROM gateway.audit_log") == "seed.action"


def test_gateway_app_can_update_other_gateway_tables(
    engine: Engine, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with _as_app_role(engine) as conn:
        result = conn.execute(
            text("UPDATE gateway.users SET email = 'renamed@example.com' WHERE id = :id"), {"id": user_id}
        )

    # Assert
    assert result.rowcount == 1


def test_gateway_app_is_denied_truncate_on_other_gateway_tables(engine: Engine) -> None:
    # Arrange / Act
    with pytest.raises(DBAPIError) as denied, _as_app_role(engine) as conn:
        conn.execute(text("TRUNCATE gateway.users CASCADE"))

    # Assert
    assert isinstance(denied.value.orig, psycopg.errors.InsufficientPrivilege)


@pytest.mark.parametrize(
    ("privilege", "expected"),
    [("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False), ("TRUNCATE", False)],
)
def test_gateway_app_privileges_on_audit_log(
    session_factory: sessionmaker[Session], privilege: str, expected: bool
) -> None:
    # Arrange / Act
    granted = _scalar(
        session_factory,
        "SELECT has_table_privilege(CAST(:role AS name), 'gateway.audit_log', CAST(:privilege AS text))",
        role=APP_ROLE,
        privilege=privilege,
    )

    # Assert
    assert granted is expected


@pytest.mark.parametrize(
    ("privilege", "expected"),
    [("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", True), ("TRUNCATE", False)],
)
def test_gateway_app_privileges_on_an_ordinary_table(
    session_factory: sessionmaker[Session], privilege: str, expected: bool
) -> None:
    # Arrange / Act
    granted = _scalar(
        session_factory,
        "SELECT has_table_privilege(CAST(:role AS name), 'gateway.users', CAST(:privilege AS text))",
        role=APP_ROLE,
        privilege=privilege,
    )

    # Assert
    assert granted is expected


def test_gateway_app_cannot_log_in_and_has_schema_and_sequence_usage(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    can_login = _scalar(session_factory, "SELECT rolcanlogin FROM pg_roles WHERE rolname = :r", r=APP_ROLE)
    usage = (
        _scalar(session_factory, GATEWAY_USAGE, r=APP_ROLE),
        _scalar(session_factory, SIMULATOR_USAGE, r=APP_ROLE),
        _scalar(
            session_factory,
            "SELECT has_sequence_privilege("
            "CAST(:r AS name), pg_get_serial_sequence('gateway.audit_log', 'id'), 'USAGE')",
            r=APP_ROLE,
        ),
    )

    # Assert
    assert can_login is False
    assert usage == (True, True, True)


def test_default_privileges_cover_tables_created_by_later_migrations(engine: Engine) -> None:
    # Arrange / Act
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            conn.execute(text("CREATE TABLE gateway.zz_later_table (id integer)"))
            granted = [
                conn.execute(
                    text(
                        "SELECT has_table_privilege("
                        "CAST(:r AS name), 'gateway.zz_later_table', CAST(:p AS text))"
                    ),
                    {"r": APP_ROLE, "p": privilege},
                ).scalar_one()
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
            ]
        finally:
            transaction.rollback()

    # Assert
    assert granted == [True, True, True, True, False]


# --- (c) use_db_role --------------------------------------------------------------------------------------


def _fresh_engine(engine: Engine) -> Engine:
    return make_engine(engine.url.render_as_string(hide_password=False))


def test_use_db_role_makes_every_pooled_connection_run_as_that_role(engine: Engine) -> None:
    # Arrange
    scoped = _fresh_engine(engine)
    try:
        use_db_role(scoped, APP_ROLE)

        # Act
        with scoped.connect() as conn:
            first = conn.execute(text("SELECT current_user")).scalar_one()
        with scoped.connect() as conn:
            second = conn.execute(text("SELECT current_user")).scalar_one()
        with engine.connect() as conn:
            unscoped = conn.execute(text("SELECT current_user")).scalar_one()
    finally:
        scoped.dispose()

    # Assert
    assert (first, second) == (APP_ROLE, APP_ROLE)
    assert unscoped != APP_ROLE


def test_use_db_role_keeps_the_role_after_a_rollback_and_across_new_connections(engine: Engine) -> None:
    # Arrange
    scoped = _fresh_engine(engine)
    try:
        use_db_role(scoped, APP_ROLE)
        with scoped.connect() as conn:
            conn.execute(text("SELECT 1"))
            conn.rollback()

        # Act
        scoped.dispose()
        with scoped.connect() as conn:
            current = conn.execute(text("SELECT current_user")).scalar_one()
    finally:
        scoped.dispose()

    # Assert
    assert current == APP_ROLE


def test_engine_scoped_to_gateway_app_cannot_update_the_audit_log(engine: Engine) -> None:
    # Arrange
    _seed_audit_row(engine)
    scoped = _fresh_engine(engine)
    try:
        use_db_role(scoped, APP_ROLE)

        # Act
        with pytest.raises(DBAPIError) as denied, scoped.begin() as conn:
            conn.execute(text("UPDATE gateway.audit_log SET action = 'tampered'"))
    finally:
        scoped.dispose()

    # Assert
    assert isinstance(denied.value.orig, psycopg.errors.InsufficientPrivilege)


@pytest.mark.parametrize("role", ["x; DROP", "gateway_app; DROP TABLE gateway.users"])
def test_use_db_role_rejects_a_role_name_that_is_not_a_plain_identifier(engine: Engine, role: str) -> None:
    # Arrange
    scoped = _fresh_engine(engine)

    # Act / Assert
    try:
        with pytest.raises(ValueError):
            use_db_role(scoped, role)
            with scoped.connect():
                pass
    finally:
        scoped.dispose()


# --- (d) a failing audit insert rolls the whole operation back ----------------------------------------------


def test_screening_commits_a_review_and_an_outbox_event_when_the_audit_insert_works(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    from gateway.compliance.kyc import OperationRequest, screen_operation

    user_id = _signed_up(session_factory, clock)

    # Act
    screen_operation(
        session_factory,
        clock,
        user_id,
        OperationRequest(kind="transfer", amount_minor=1_000_000, currency="USD"),
    )

    # Assert
    assert _count(session_factory, "gateway.kyc_reviews") == 1
    assert _scalar(session_factory, "SELECT kyc_status FROM gateway.users WHERE id = :id", id=user_id) == (
        "pending"
    )


def test_screening_rolls_back_the_review_the_status_change_and_the_outbox_event_when_audit_fails(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    from gateway.compliance.kyc import OperationRequest, screen_operation

    user_id = _signed_up(session_factory, clock)
    outbox_before = _count(session_factory, "gateway.outbox")
    monkeypatch.setattr("gateway.compliance.kyc.KYC_REVIEW_OPENED_ACTION", OVERLONG_ACTION)

    # Act
    with pytest.raises(DBAPIError):
        screen_operation(
            session_factory,
            clock,
            user_id,
            OperationRequest(kind="transfer", amount_minor=1_000_000, currency="USD"),
        )

    # Assert
    status = _scalar(session_factory, "SELECT kyc_status FROM gateway.users WHERE id = :id", id=user_id)
    assert status == "unverified"
    assert _count(session_factory, "gateway.kyc_reviews") == 0
    assert _count(session_factory, "gateway.outbox") == outbox_before


def test_rectification_completes_with_a_request_row_when_the_audit_insert_works(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    from gateway.compliance.gdpr import rectify_profile

    user_id = _signed_up(session_factory, clock)

    # Act
    rectify_profile(session_factory, clock, user_id, full_name="New Name", country=None)

    # Assert
    name = _scalar(session_factory, "SELECT full_name FROM gateway.users WHERE id = :id", id=user_id)
    kinds = _scalar(
        session_factory, "SELECT string_agg(kind || ':' || status, ',') FROM gateway.data_subject_requests"
    )
    assert name == "New Name"
    assert kinds == "rectification:completed"


def test_rectification_rolls_back_the_profile_change_and_the_request_row_when_audit_fails(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    from gateway.compliance.gdpr import rectify_profile

    user_id = _signed_up(session_factory, clock)
    monkeypatch.setattr("gateway.compliance.gdpr.RECTIFIED_ACTION", OVERLONG_ACTION)

    # Act
    with pytest.raises(DBAPIError):
        rectify_profile(session_factory, clock, user_id, full_name="New Name", country=None)

    # Assert
    name = _scalar(session_factory, "SELECT full_name FROM gateway.users WHERE id = :id", id=user_id)
    assert name is None
    assert _count(session_factory, "gateway.data_subject_requests") == 0


# --- (e) record_audit participates in the caller's transaction ----------------------------------------------


def test_record_audit_row_is_visible_after_commit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    with session_factory() as session:
        record_audit(session, clock, "user:alice", "consumer", "test.committed", "user:alice", {"k": 1})
        session.commit()

    # Assert
    with session_factory() as session:
        rows = [
            dict(row)
            for row in session.execute(
                text(
                    "SELECT actor_id, actor_role, action, target, summary, occurred_at FROM gateway.audit_log"
                )
            ).mappings()
        ]
    assert rows == [
        {
            "actor_id": "user:alice",
            "actor_role": "consumer",
            "action": "test.committed",
            "target": "user:alice",
            "summary": {"k": 1},
            "occurred_at": clock(),
        }
    ]


def test_record_audit_row_is_absent_after_rollback(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    with session_factory() as session:
        record_audit(session, clock, "user:alice", "consumer", "test.rolled_back", "user:alice", {})
        in_transaction = session.execute(text("SELECT count(*) FROM gateway.audit_log")).scalar_one()
        session.rollback()

    # Assert
    assert in_transaction == 1
    assert _count(session_factory, "gateway.audit_log") == 0


def test_record_audit_does_not_commit_on_its_own(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as writer:
        record_audit(writer, clock, "user:alice", "consumer", "test.pending", "user:alice", {})

        # Act
        visible_elsewhere = _count(session_factory, "gateway.audit_log")
        writer.rollback()

    # Assert
    assert visible_elsewhere == 0


def test_record_audit_with_an_overlong_action_raises_when_called(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act / Assert
        with pytest.raises(DBAPIError):
            record_audit(session, clock, "user:alice", "consumer", OVERLONG_ACTION, "user:alice", {})
