"""Tests for gateway/wallets/service.py, written from docs/specs/gateway-basic.md section B1."""

import uuid

from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from gateway.payments.ledger.service import ensure_account
from gateway.wallets.models import AccountBalance
from gateway.wallets.service import list_wallets
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"


def _user(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str, currency: str = "USD"
) -> tuple[uuid.UUID, uuid.UUID]:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD, currency=currency)
        session.commit()
    return result.user_id, result.wallet_account_id


def _add_wallet(
    session_factory: sessionmaker[Session],
    user_id: uuid.UUID,
    currency: str,
    available_minor: int,
    held_minor: int,
) -> uuid.UUID:
    with session_factory.begin() as session:
        account_id = ensure_account(session, "user", user_id, "wallet", currency)
        session.add(
            AccountBalance(account_id=account_id, available_minor=available_minor, held_minor=held_minor)
        )
    return account_id


def _set_balance(
    session_factory: sessionmaker[Session], account_id: uuid.UUID, available_minor: int, held_minor: int
) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(AccountBalance)
            .where(AccountBalance.account_id == account_id)
            .values(available_minor=available_minor, held_minor=held_minor)
        )


def test_list_wallets_returns_the_signup_wallet_with_a_zero_balance(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, "alice@example.com")

    # Act
    with session_factory() as session:
        views = list_wallets(session, user_id)

    # Assert
    assert [(v.account_id, v.currency, v.available_minor, v.held_minor) for v in views] == [
        (wallet_id, "USD", 0, 0)
    ]


def test_list_wallets_reads_available_and_held_from_the_balance_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, "alice@example.com")
    _set_balance(session_factory, wallet_id, available_minor=12_345, held_minor=678)

    # Act
    with session_factory() as session:
        views = list_wallets(session, user_id)

    # Assert
    assert [(v.account_id, v.currency, v.available_minor, v.held_minor) for v in views] == [
        (wallet_id, "USD", 12_345, 678)
    ]


def test_list_wallets_orders_by_currency_whatever_the_creation_order(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, usd_id = _user(session_factory, clock, "alice@example.com", currency="USD")
    jpy_id = _add_wallet(session_factory, user_id, "JPY", 500, 5)
    eur_id = _add_wallet(session_factory, user_id, "EUR", 700, 7)
    gbp_id = _add_wallet(session_factory, user_id, "GBP", 900, 9)

    # Act
    with session_factory() as session:
        views = list_wallets(session, user_id)

    # Assert
    assert [(v.account_id, v.currency, v.available_minor, v.held_minor) for v in views] == [
        (eur_id, "EUR", 700, 7),
        (gbp_id, "GBP", 900, 9),
        (jpy_id, "JPY", 500, 5),
        (usd_id, "USD", 0, 0),
    ]


def test_list_wallets_returns_only_the_requested_users_wallets(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, "alice@example.com")
    bob, bob_wallet = _user(session_factory, clock, "bob@example.com")
    _set_balance(session_factory, alice_wallet, available_minor=100, held_minor=1)
    _set_balance(session_factory, bob_wallet, available_minor=999, held_minor=9)
    bob_eur = _add_wallet(session_factory, bob, "EUR", 5, 0)

    # Act
    with session_factory() as session:
        alice_views = list_wallets(session, alice)
        bob_views = list_wallets(session, bob)

    # Assert
    assert [(v.account_id, v.available_minor, v.held_minor) for v in alice_views] == [(alice_wallet, 100, 1)]
    assert [(v.account_id, v.currency, v.available_minor) for v in bob_views] == [
        (bob_eur, "EUR", 5),
        (bob_wallet, "USD", 999),
    ]


def test_list_wallets_skips_accounts_of_the_user_that_are_not_wallets(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, "alice@example.com")
    with session_factory.begin() as session:
        ensure_account(session, "user", user_id, "escrow", "EUR")

    # Act
    with session_factory() as session:
        views = list_wallets(session, user_id)

    # Assert
    assert [(v.account_id, v.currency) for v in views] == [(wallet_id, "USD")]


def test_list_wallets_returns_an_empty_list_for_an_unknown_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _user(session_factory, clock, "alice@example.com")

    # Act
    with session_factory() as session:
        views = list_wallets(session, uuid.UUID("deadbeef-0000-4000-8000-000000000000"))

    # Assert
    assert views == []


def test_list_wallets_reflects_a_balance_change_made_after_the_first_read(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, "alice@example.com")
    with session_factory() as session:
        before = [(v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]
    _set_balance(session_factory, wallet_id, available_minor=42, held_minor=2)

    # Act
    with session_factory() as session:
        after = [(v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]

    # Assert
    assert (before, after) == ([(0, 0)], [(42, 2)])
