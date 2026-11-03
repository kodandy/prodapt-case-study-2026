"""Tests for bootstrap_merchant(merchant_id), written from docs/specs/sq1.md section 1 (gateway/admin.py)."""

import uuid

from gateway.accounts.service import authenticate_api_key
from gateway.admin import bootstrap_merchant
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER

CHOSEN_ID = uuid.UUID("11111111-2222-4333-8444-555555555555")


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def test_bootstrap_merchant_with_a_merchant_id_creates_the_merchant_under_that_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: empty merchants table from the fixture

    # Act
    credentials = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER, CHOSEN_ID)

    # Assert
    assert credentials.merchant_id == CHOSEN_ID
    assert _rows(session_factory, "SELECT id, name FROM gateway.merchants") == [
        {"id": CHOSEN_ID, "name": "Acme Demo"}
    ]
    with session_factory() as session:
        assert authenticate_api_key(session, credentials.api_key, TEST_API_KEY_PEPPER) == CHOSEN_ID


def test_bootstrap_merchant_with_an_existing_merchant_id_adds_a_key_and_no_second_merchant_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER, CHOSEN_ID)

    # Act
    second = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER, CHOSEN_ID)

    # Assert
    assert second.merchant_id == CHOSEN_ID
    assert second.api_key != first.api_key
    assert _rows(session_factory, "SELECT id FROM gateway.merchants") == [{"id": CHOSEN_ID}]
    assert _rows(session_factory, "SELECT merchant_id FROM gateway.api_keys") == [
        {"merchant_id": CHOSEN_ID},
        {"merchant_id": CHOSEN_ID},
    ]


def test_bootstrap_merchant_with_an_existing_merchant_id_keeps_both_keys_working(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER, CHOSEN_ID)
    second = bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER, CHOSEN_ID)

    # Act
    with session_factory() as session:
        first_owner = authenticate_api_key(session, first.api_key, TEST_API_KEY_PEPPER)
        second_owner = authenticate_api_key(session, second.api_key, TEST_API_KEY_PEPPER)

    # Assert
    assert (first_owner, second_owner) == (CHOSEN_ID, CHOSEN_ID)


def test_bootstrap_merchant_with_a_different_merchant_id_creates_a_second_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    other_id = uuid.UUID("aaaaaaaa-2222-4333-8444-555555555555")
    bootstrap_merchant(session_factory, clock, "Acme Demo", TEST_API_KEY_PEPPER, CHOSEN_ID)

    # Act
    other = bootstrap_merchant(session_factory, clock, "Globex Demo", TEST_API_KEY_PEPPER, other_id)

    # Assert
    assert other.merchant_id == other_id
    assert _rows(session_factory, "SELECT id FROM gateway.merchants ORDER BY id") == [
        {"id": CHOSEN_ID},
        {"id": other_id},
    ]
