"""Tests for create_merchant(merchant_id) and issue_api_key, written from docs/specs/sq1.md section 1."""

import uuid

import pytest
from gateway.accounts.service import (
    API_KEY_PREFIX,
    MerchantCredentials,
    UnknownMerchantError,
    authenticate_api_key,
    create_merchant,
    issue_api_key,
)
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER

CHOSEN_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _create_committed(
    session_factory: sessionmaker[Session], clock: FixedClock, merchant_id: uuid.UUID | None = None
) -> MerchantCredentials:
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER, merchant_id)
        session.commit()
    return credentials


def test_create_merchant_with_a_merchant_id_stores_the_row_under_that_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    credentials = _create_committed(session_factory, clock, CHOSEN_ID)

    # Assert
    assert credentials.merchant_id == CHOSEN_ID
    assert _rows(session_factory, "SELECT id, name FROM gateway.merchants") == [
        {"id": CHOSEN_ID, "name": "Acme"}
    ]


def test_create_merchant_with_a_merchant_id_issues_a_key_that_authenticates_as_that_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    credentials = _create_committed(session_factory, clock, CHOSEN_ID)

    # Act
    with session_factory() as session:
        authenticated = authenticate_api_key(session, credentials.api_key, TEST_API_KEY_PEPPER)

    # Assert
    assert authenticated == CHOSEN_ID
    assert _rows(session_factory, "SELECT merchant_id FROM gateway.api_keys") == [{"merchant_id": CHOSEN_ID}]


def test_create_merchant_without_a_merchant_id_generates_a_random_uuid4(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    first = _create_committed(session_factory, clock)
    second = _create_committed(session_factory, clock, None)

    # Assert
    assert first.merchant_id.version == 4
    assert second.merchant_id.version == 4
    assert first.merchant_id != second.merchant_id


def test_issue_api_key_returns_a_new_test_key_for_the_existing_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _create_committed(session_factory, clock, CHOSEN_ID)

    # Act
    with session_factory() as session:
        second = issue_api_key(session, clock, CHOSEN_ID, TEST_API_KEY_PEPPER)
        session.commit()

    # Assert
    assert isinstance(second, MerchantCredentials)
    assert second.merchant_id == CHOSEN_ID
    assert second.api_key.startswith(API_KEY_PREFIX)
    assert second.api_key != first.api_key
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.api_keys") == [{"n": 2}]
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.merchants") == [{"n": 1}]


def test_issue_api_key_keeps_earlier_keys_working_and_authenticates_the_new_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _create_committed(session_factory, clock, CHOSEN_ID)
    with session_factory() as session:
        second = issue_api_key(session, clock, CHOSEN_ID, TEST_API_KEY_PEPPER)
        session.commit()

    # Act
    with session_factory() as session:
        first_owner = authenticate_api_key(session, first.api_key, TEST_API_KEY_PEPPER)
        second_owner = authenticate_api_key(session, second.api_key, TEST_API_KEY_PEPPER)

    # Assert
    assert (first_owner, second_owner) == (CHOSEN_ID, CHOSEN_ID)


def test_issue_api_key_stores_only_a_hash_of_the_new_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _create_committed(session_factory, clock, CHOSEN_ID)

    # Act
    with session_factory() as session:
        issued = issue_api_key(session, clock, CHOSEN_ID, TEST_API_KEY_PEPPER)
        session.commit()

    # Assert
    stored = _rows(session_factory, "SELECT key_hash, prefix FROM gateway.api_keys")
    assert len(stored) == 2
    assert [issued.api_key in str(row["key_hash"]) for row in stored] == [False, False]
    assert [issued.api_key == row["prefix"] for row in stored] == [False, False]


def test_issue_api_key_for_an_unknown_merchant_raises_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _create_committed(session_factory, clock, CHOSEN_ID)
    unknown = uuid.UUID("99999999-2222-4333-8444-555555555555")

    # Act
    with session_factory() as session, pytest.raises(UnknownMerchantError):
        issue_api_key(session, clock, unknown, TEST_API_KEY_PEPPER)
        session.commit()

    # Assert
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.api_keys") == [{"n": 1}]


def test_issue_api_key_does_not_commit(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    _create_committed(session_factory, clock, CHOSEN_ID)

    # Act
    with session_factory() as session:
        issue_api_key(session, clock, CHOSEN_ID, TEST_API_KEY_PEPPER)
        session.rollback()

    # Assert
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.api_keys") == [{"n": 1}]


def test_issue_api_key_key_is_rejected_under_a_different_pepper(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _create_committed(session_factory, clock, CHOSEN_ID)
    with session_factory() as session:
        issued = issue_api_key(session, clock, CHOSEN_ID, TEST_API_KEY_PEPPER)
        session.commit()

    # Act
    with session_factory() as session:
        authenticated = authenticate_api_key(session, issued.api_key, "another-pepper")

    # Assert
    assert authenticated is None
