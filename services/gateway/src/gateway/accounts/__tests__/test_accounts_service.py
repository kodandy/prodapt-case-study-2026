import uuid

import pytest
from gateway.accounts.service import (
    EmailAlreadyRegisteredError,
    authenticate_api_key,
    create_merchant,
    hash_password,
    sign_up,
    verify_password,
)
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER

PASSWORD = "correct horse battery staple"


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def test_hash_password_produces_an_argon2id_hash_that_verifies() -> None:
    # Arrange / Act
    password_hash = hash_password(PASSWORD)

    # Assert
    assert password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash
    assert verify_password(password_hash, PASSWORD) is True


def test_verify_password_rejects_a_wrong_password() -> None:
    # Arrange
    password_hash = hash_password(PASSWORD)

    # Act
    result = verify_password(password_hash, "not the password")

    # Assert
    assert result is False


def test_sign_up_stores_an_argon2id_hash_and_never_the_plaintext(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = sign_up(session, clock, "alice@example.com", PASSWORD)
        session.commit()

    # Assert
    users = _rows(session_factory, "SELECT id, email, password_hash FROM gateway.users")
    assert len(users) == 1
    assert users[0]["id"] == result.user_id
    assert users[0]["email"] == "alice@example.com"
    stored_hash = str(users[0]["password_hash"])
    assert stored_hash.startswith("$argon2id$")
    assert PASSWORD not in stored_hash
    assert verify_password(stored_hash, PASSWORD) is True


def test_sign_up_creates_one_wallet_account_with_a_zero_balance(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = sign_up(session, clock, "alice@example.com", PASSWORD, currency="EUR")
        session.commit()

    # Assert
    accounts = _rows(session_factory, "SELECT id, owner_id, kind, currency FROM gateway.ledger_accounts")
    balances = _rows(
        session_factory, "SELECT account_id, available_minor, held_minor FROM gateway.account_balances"
    )
    assert accounts == [
        {"id": result.wallet_account_id, "owner_id": result.user_id, "kind": "wallet", "currency": "EUR"}
    ]
    assert balances == [{"account_id": result.wallet_account_id, "available_minor": 0, "held_minor": 0}]


def test_sign_up_defaults_the_wallet_currency_to_usd(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        sign_up(session, clock, "alice@example.com", PASSWORD)
        session.commit()

    # Assert
    currencies = _rows(session_factory, "SELECT currency FROM gateway.ledger_accounts")
    assert currencies == [{"currency": "USD"}]


@pytest.mark.parametrize("second_email", ["alice@example.com", "ALICE@Example.COM"])
def test_sign_up_rejects_a_duplicate_email_case_insensitively(
    session_factory: sessionmaker[Session], clock: FixedClock, second_email: str
) -> None:
    # Arrange
    with session_factory() as session:
        sign_up(session, clock, "alice@example.com", PASSWORD)
        session.commit()

    # Act
    with session_factory() as session, pytest.raises(EmailAlreadyRegisteredError):
        sign_up(session, clock, second_email, PASSWORD)
        session.commit()

    # Assert
    users = _rows(session_factory, "SELECT email FROM gateway.users")
    assert users == [{"email": "alice@example.com"}]


def test_create_merchant_returns_a_test_key_stored_only_as_a_hash(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()

    # Assert
    merchants = _rows(session_factory, "SELECT id, name FROM gateway.merchants")
    keys = _rows(session_factory, "SELECT merchant_id, key_hash FROM gateway.api_keys")
    assert credentials.api_key.startswith("sk_test_")
    assert merchants == [{"id": credentials.merchant_id, "name": "Acme"}]
    assert len(keys) == 1
    assert keys[0]["merchant_id"] == credentials.merchant_id
    stored_hash = str(keys[0]["key_hash"])
    assert stored_hash != credentials.api_key
    assert credentials.api_key not in stored_hash


def test_authenticate_api_key_returns_the_merchant_id_for_its_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        first = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        second = create_merchant(session, clock, "Globex", TEST_API_KEY_PEPPER)
        session.commit()

    # Act
    with session_factory() as session:
        found_first = authenticate_api_key(session, first.api_key, TEST_API_KEY_PEPPER)
        found_second = authenticate_api_key(session, second.api_key, TEST_API_KEY_PEPPER)

    # Assert
    assert found_first == first.merchant_id
    assert found_second == second.merchant_id
    assert first.api_key != second.api_key


@pytest.mark.parametrize("presented", ["", "sk_test_unknown", "not-even-a-key"])
def test_authenticate_api_key_returns_none_for_any_other_key(
    session_factory: sessionmaker[Session], clock: FixedClock, presented: str
) -> None:
    # Arrange
    with session_factory() as session:
        create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()

    # Act
    with session_factory() as session:
        result = authenticate_api_key(session, presented, TEST_API_KEY_PEPPER)

    # Assert
    assert result is None


def test_authenticate_api_key_returns_none_when_the_pepper_differs(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()

    # Act
    with session_factory() as session:
        result = authenticate_api_key(session, credentials.api_key, "another-pepper")

    # Assert
    assert result is None
    assert isinstance(credentials.merchant_id, uuid.UUID)
