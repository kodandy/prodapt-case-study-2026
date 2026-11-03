"""Tests for gateway/admin.py, written from docs/specs/runtime.md (R3 bootstrap_merchant)."""

from gateway.accounts.service import API_KEY_PREFIX, authenticate_api_key
from gateway.admin import bootstrap_merchant
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def test_bootstrap_merchant_commits_the_merchant_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: empty merchants table from the fixture

    # Act
    credentials = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER)

    # Assert
    assert _rows(session_factory, "SELECT id, name, created_at FROM gateway.merchants") == [
        {"id": credentials.merchant_id, "name": "Acme Demo", "created_at": clock()}
    ]


def test_bootstrap_merchant_returns_an_api_key_that_authenticates_as_that_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    credentials = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER)

    # Act
    with session_factory() as session:
        authenticated = authenticate_api_key(session, credentials.api_key, TEST_API_KEY_PEPPER)

    # Assert
    assert credentials.api_key.startswith(API_KEY_PREFIX)
    assert authenticated == credentials.merchant_id


def test_bootstrap_merchant_key_is_rejected_under_a_different_pepper(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    credentials = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER)

    # Act
    with session_factory() as session:
        authenticated = authenticate_api_key(session, credentials.api_key, "another-pepper-value")

    # Assert
    assert authenticated is None


def test_bootstrap_merchant_stores_only_a_hash_of_the_api_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    credentials = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER)

    # Act
    stored = _rows(session_factory, "SELECT merchant_id, key_hash, prefix FROM gateway.api_keys")

    # Assert
    assert len(stored) == 1
    assert stored[0]["merchant_id"] == credentials.merchant_id
    assert credentials.api_key not in str(stored[0]["key_hash"])
    assert credentials.api_key != stored[0]["prefix"]


def test_bootstrap_merchant_second_call_creates_another_merchant_with_a_different_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER)

    # Act
    second = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER)

    # Assert
    assert first.merchant_id != second.merchant_id
    assert first.api_key != second.api_key
    with session_factory() as session:
        first_owner = authenticate_api_key(session, first.api_key, TEST_API_KEY_PEPPER)
        second_owner = authenticate_api_key(session, second.api_key, TEST_API_KEY_PEPPER)
    assert (first_owner, second_owner) == (first.merchant_id, second.merchant_id)
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.merchants") == [{"n": 2}]
