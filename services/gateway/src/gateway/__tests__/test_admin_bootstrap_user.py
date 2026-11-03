"""Tests for bootstrap_user, written from docs/specs/sq2.md section 1 (gateway/admin.py)."""

import uuid

from gateway.accounts.service import authenticate_user
from gateway.admin import bootstrap_user
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

EMAIL = "sq2-sender@example.com"
USER_ID = uuid.UUID("11111111-2222-4333-8444-666666666666")
FIRST_PASSWORD = "first-random-password-1"
SECOND_PASSWORD = "second-random-password-2"


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def test_bootstrap_user_creates_the_user_under_the_given_id_and_returns_it(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: empty users table from the fixture

    # Act
    returned = bootstrap_user(session_factory, clock, EMAIL, FIRST_PASSWORD, USER_ID)

    # Assert
    assert returned == USER_ID
    assert _rows(session_factory, "SELECT id, email FROM gateway.users") == [{"id": USER_ID, "email": EMAIL}]


def test_bootstrap_user_commits_so_the_new_user_can_log_in(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bootstrap_user(session_factory, clock, EMAIL, FIRST_PASSWORD, USER_ID)

    # Act
    with session_factory() as session:
        result = authenticate_user(session, EMAIL, FIRST_PASSWORD)

    # Assert
    assert result == USER_ID


def test_bootstrap_user_gives_the_new_user_one_wallet_account(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    bootstrap_user(session_factory, clock, EMAIL, FIRST_PASSWORD, USER_ID)

    # Assert
    assert _rows(session_factory, "SELECT owner_id, kind FROM gateway.ledger_accounts") == [
        {"owner_id": USER_ID, "kind": "wallet"}
    ]


def test_bootstrap_user_for_an_existing_id_replaces_the_password(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bootstrap_user(session_factory, clock, EMAIL, FIRST_PASSWORD, USER_ID)

    # Act
    returned = bootstrap_user(session_factory, clock, EMAIL, SECOND_PASSWORD, USER_ID)

    # Assert
    assert returned == USER_ID
    with session_factory() as session:
        assert authenticate_user(session, EMAIL, FIRST_PASSWORD) is None
        assert authenticate_user(session, EMAIL, SECOND_PASSWORD) == USER_ID


def test_bootstrap_user_for_an_existing_id_leaves_one_user_row_and_one_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bootstrap_user(session_factory, clock, EMAIL, FIRST_PASSWORD, USER_ID)

    # Act
    bootstrap_user(session_factory, clock, EMAIL, SECOND_PASSWORD, USER_ID)

    # Assert
    assert _rows(session_factory, "SELECT id FROM gateway.users") == [{"id": USER_ID}]
    assert _rows(session_factory, "SELECT owner_id FROM gateway.ledger_accounts") == [{"owner_id": USER_ID}]


def test_bootstrap_user_with_a_different_id_creates_a_second_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    other_id = uuid.UUID("aaaaaaaa-2222-4333-8444-666666666666")
    bootstrap_user(session_factory, clock, EMAIL, FIRST_PASSWORD, USER_ID)

    # Act
    bootstrap_user(session_factory, clock, "other@example.com", SECOND_PASSWORD, other_id)

    # Assert
    assert _rows(session_factory, "SELECT id FROM gateway.users ORDER BY id") == [
        {"id": USER_ID},
        {"id": other_id},
    ]
    with session_factory() as session:
        assert authenticate_user(session, EMAIL, FIRST_PASSWORD) == USER_ID
        assert authenticate_user(session, "other@example.com", SECOND_PASSWORD) == other_id
