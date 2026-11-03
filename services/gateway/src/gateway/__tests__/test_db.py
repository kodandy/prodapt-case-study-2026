import os
import uuid

import pytest
from gateway.clock import FixedClock
from gateway.db import make_engine
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

PASSWORD_HASH = "$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$aGFzaC1zZW50aW5lbC1ub3QtYS1yZWFsLWhhc2g"
INSERT_USER = (
    "INSERT INTO gateway.users (id, email, password_hash, created_at) "
    "VALUES (:id, :email, :password_hash, :created_at)"
)


def _insert_user(session: Session, clock: FixedClock, email: str, password_hash: str) -> None:
    session.execute(
        text(INSERT_USER),
        {"id": uuid.uuid4(), "email": email, "password_hash": password_hash, "created_at": clock()},
    )


def test_make_engine_hides_sql_parameters() -> None:
    # Arrange
    engine = make_engine("postgresql+psycopg://user:pw@localhost:5432/db")

    # Act
    hidden = engine.hide_parameters

    # Assert
    assert hidden is True
    engine.dispose()


def test_duplicate_email_error_does_not_contain_the_password_hash_or_bound_parameters(
    engine: Engine, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    email = "hidden.params@example.com"
    with session_factory() as session:
        _insert_user(session, clock, email, PASSWORD_HASH)
        session.commit()

    # Act
    with session_factory() as session, pytest.raises(DBAPIError) as raised:
        _insert_user(session, clock, email, PASSWORD_HASH)

    # Assert
    message = str(raised.value)
    assert isinstance(raised.value, IntegrityError)
    assert "uq_users_email" in message
    assert PASSWORD_HASH not in message
    assert "$argon2id$" not in message
    assert "[SQL parameters hidden due to hide_parameters=True]" in message
    assert "[parameters:" not in message


def test_error_with_a_rejected_value_does_not_contain_the_email_or_the_password_hash(
    engine: Engine, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: an over-long hash makes Postgres fail without echoing any value in its own message
    email = "overlong.hash@example.com"
    overlong_hash = "$argon2id$" + "x" * 300

    # Act
    with session_factory() as session, pytest.raises(DBAPIError) as raised:
        _insert_user(session, clock, email, overlong_hash)

    # Assert
    message = str(raised.value)
    assert email not in message
    assert "$argon2id$" not in message
    assert overlong_hash not in message
    assert os.environ["DATABASE_URL"] not in message
