"""Tests for `transfer`, `top_up_wallet`, `ensure_wallet` and the `Transfer` model, written from
docs/specs/gateway-basic.md section B2."""

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from gateway.wallets.models import AccountBalance, Transfer
from gateway.wallets.service import (
    TransferOutcome,
    ensure_wallet,
    list_wallets,
    top_up_wallet,
    transfer,
)
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
PARALLEL = 8


def _user(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str, currency: str = "USD"
) -> tuple[uuid.UUID, uuid.UUID]:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD, currency=currency)
        session.commit()
    return result.user_id, result.wallet_account_id


def _fund(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    currency: str,
    amount_minor: int,
) -> uuid.UUID:
    with session_factory.begin() as session:
        return top_up_wallet(session, clock, user_id, currency, amount_minor)


def _set_balance(
    session_factory: sessionmaker[Session], account_id: uuid.UUID, available_minor: int, held_minor: int
) -> None:
    with session_factory.begin() as session:
        session.execute(
            update(AccountBalance)
            .where(AccountBalance.account_id == account_id)
            .values(available_minor=available_minor, held_minor=held_minor)
        )


def _send(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender: uuid.UUID,
    key: str,
    recipient_email: str,
    amount_minor: int,
    currency: str = "USD",
) -> TransferOutcome:
    return transfer(
        session_factory,
        clock,
        sender_user_id=sender,
        idempotency_key=key,
        recipient_email=recipient_email,
        amount_minor=amount_minor,
        currency=currency,
    )


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str, **params: object) -> int:
    return int(str(_rows(session_factory, query, **params)[0]["n"]))


def _transfer_count(session_factory: sessionmaker[Session]) -> int:
    return _count(session_factory, "SELECT count(*) AS n FROM gateway.transfers")


def _journal_count(session_factory: sessionmaker[Session], ref_type: str) -> int:
    return _count(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = :ref_type",
        ref_type=ref_type,
    )


def _audit_count(session_factory: sessionmaker[Session]) -> int:
    return _count(
        session_factory, "SELECT count(*) AS n FROM gateway.audit_log WHERE action LIKE 'transfer.%'"
    )


def _journal_lines(session_factory: sessionmaker[Session], ref_type: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT j.ref_type, j.ref_id, p.account_id, p.currency, p.amount_minor "
        "FROM gateway.journal_entries j JOIN gateway.postings p ON p.journal_id = j.id "
        "WHERE j.ref_type = :ref_type ORDER BY p.amount_minor DESC, p.account_id",
        ref_type=ref_type,
    )


def _ledger_sum_by_currency(session_factory: sessionmaker[Session]) -> dict[str, int]:
    rows = _rows(
        session_factory,
        "SELECT currency, sum(amount_minor) AS total FROM gateway.postings GROUP BY currency",
    )
    return {str(row["currency"]): int(str(row["total"])) for row in rows}


def _wallet_ledger(session_factory: sessionmaker[Session]) -> dict[uuid.UUID, tuple[int, int]]:
    """Per user wallet: (available + held, minus the sum of its postings)."""
    rows = _rows(
        session_factory,
        "SELECT la.id AS id, (b.available_minor + b.held_minor)::bigint AS balance, "
        "(-COALESCE(SUM(p.amount_minor), 0))::bigint AS posted "
        "FROM gateway.ledger_accounts la "
        "JOIN gateway.account_balances b ON b.account_id = la.id "
        "LEFT JOIN gateway.postings p ON p.account_id = la.id "
        "WHERE la.owner_type = 'user' AND la.kind = 'wallet' "
        "GROUP BY la.id, b.available_minor, b.held_minor",
    )
    return {uuid.UUID(str(row["id"])): (int(str(row["balance"])), int(str(row["posted"]))) for row in rows}


def _available(session_factory: sessionmaker[Session], user_id: uuid.UUID, currency: str) -> int:
    with session_factory() as session:
        views = [v for v in list_wallets(session, user_id) if v.currency == currency]
    return views[0].available_minor


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _transfer_id(outcome: TransferOutcome) -> uuid.UUID:
    return uuid.UUID(str(outcome.body["transfer_id"]))


def _run_parallel[T](jobs: Sequence[Callable[[], T]]) -> list[T]:
    barrier = threading.Barrier(len(jobs))

    def run(job: Callable[[], T]) -> T:
        barrier.wait(timeout=30)
        return job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [future.result(timeout=30) for future in futures]


# --- transfer: happy path ---------------------------------------------------------------------------------


def test_transfer_moves_the_amount_between_the_wallets_and_returns_the_201_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == {
        "transfer_id": str(_transfer_id(outcome)),
        "state": "succeeded",
        "amount_minor": 2_500,
        "currency": "USD",
        "sender_available_minor": 7_500,
    }
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _wallets(session_factory, bob) == [("USD", 2_500, 0)]


def test_transfer_keeps_available_plus_held_equal_to_minus_the_postings_for_every_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    _send(session_factory, clock, alice, "k-1", BOB, 2_500)
    _send(session_factory, clock, alice, "k-2", BOB, 1_000)

    # Assert
    assert _wallet_ledger(session_factory) == {alice_wallet: (6_500, 6_500), bob_wallet: (3_500, 3_500)}
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


def test_transfer_posts_one_two_line_journal_with_ref_type_transfer_and_the_transfer_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Assert
    transfer_id = _transfer_id(outcome)
    assert _journal_count(session_factory, "transfer") == 1
    assert _journal_lines(session_factory, "transfer") == [
        {
            "ref_type": "transfer",
            "ref_id": transfer_id,
            "account_id": alice_wallet,
            "currency": "USD",
            "amount_minor": 2_500,
        },
        {
            "ref_type": "transfer",
            "ref_id": transfer_id,
            "account_id": bob_wallet,
            "currency": "USD",
            "amount_minor": -2_500,
        },
    ]


def test_transfer_persists_a_succeeded_transfer_row_holding_the_returned_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Assert
    with session_factory() as session:
        rows = session.scalars(select(Transfer)).all()
        stored = [
            (
                row.id,
                row.sender_user_id,
                row.recipient_user_id,
                row.sender_account_id,
                row.recipient_account_id,
                row.amount_minor,
                row.currency,
                row.state,
                row.idempotency_key,
                row.response_body,
                row.created_at,
            )
            for row in rows
        ]
    assert stored == [
        (
            _transfer_id(outcome),
            alice,
            bob,
            alice_wallet,
            bob_wallet,
            2_500,
            "USD",
            "succeeded",
            "k-1",
            outcome.body,
            clock(),
        )
    ]


def test_transfer_adds_to_an_existing_recipient_balance_across_transfers(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _fund(session_factory, clock, bob, "USD", 700)

    # Act
    first = _send(session_factory, clock, alice, "k-1", BOB, 2_500)
    second = _send(session_factory, clock, alice, "k-2", BOB, 1_500)

    # Assert
    assert (first.body["sender_available_minor"], second.body["sender_available_minor"]) == (7_500, 6_000)
    assert _wallets(session_factory, bob) == [("USD", 4_700, 0)]
    assert _wallets(session_factory, alice) == [("USD", 6_000, 0)]


def test_transfer_of_the_whole_available_balance_succeeds_and_leaves_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 1_000)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["sender_available_minor"] == 0
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 1_000, 0)]


def test_transfer_debits_only_the_wallet_of_the_requested_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 5_000)
    _fund(session_factory, clock, alice, "EUR", 3_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 1_000, currency="EUR")

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["sender_available_minor"] == 2_000
    assert _wallets(session_factory, alice) == [("EUR", 2_000, 0), ("USD", 5_000, 0)]
    assert _wallets(session_factory, bob) == [("EUR", 1_000, 0), ("USD", 0, 0)]


def test_transfer_normalizes_the_recipient_email_by_stripping_and_lowercasing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", "  Bob@Example.COM \t", 400)

    # Assert
    assert outcome.status_code == 201
    with session_factory() as session:
        recipients = session.scalars(select(Transfer.recipient_user_id)).all()
    assert recipients == [bob]
    assert _wallets(session_factory, bob) == [("USD", 400, 0)]


# --- transfer: recipient wallet in a new currency ---------------------------------------------------------


def test_transfer_creates_the_recipient_wallet_when_they_have_none_in_that_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, bob_usd_wallet = _user(session_factory, clock, BOB)
    alice_eur_wallet = _fund(session_factory, clock, alice, "EUR", 5_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 1_200, currency="EUR")

    # Assert
    assert outcome.status_code == 201
    assert _wallets(session_factory, bob) == [("EUR", 1_200, 0), ("USD", 0, 0)]
    with session_factory() as session:
        bob_eur_wallet = next(v.account_id for v in list_wallets(session, bob) if v.currency == "EUR")
        row = session.scalars(select(Transfer)).one()
        accounts = (row.sender_account_id, row.recipient_account_id)
    assert bob_eur_wallet != bob_usd_wallet
    assert accounts == (alice_eur_wallet, bob_eur_wallet)
    assert _wallet_ledger(session_factory)[bob_eur_wallet] == (1_200, 1_200)


def test_transfer_creates_the_recipient_wallet_as_a_user_wallet_account_in_that_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "JPY", 9_000)

    # Act
    _send(session_factory, clock, alice, "k-1", BOB, 9_000, currency="JPY")

    # Assert
    accounts = _rows(
        session_factory,
        "SELECT owner_type, owner_id, kind, currency FROM gateway.ledger_accounts "
        "WHERE owner_id = :id AND currency = 'JPY'",
        id=bob,
    )
    assert accounts == [{"owner_type": "user", "owner_id": bob, "kind": "wallet", "currency": "JPY"}]


# --- transfer: rejections ---------------------------------------------------------------------------------


def test_transfer_from_a_sender_with_no_wallet_in_that_currency_is_insufficient_funds_with_zero_available(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 5_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 100, currency="EUR")

    # Assert
    assert (outcome.status_code, outcome.body) == (409, {"error": "insufficient_funds", "available_minor": 0})
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]


def test_transfer_from_a_zero_balance_wallet_is_insufficient_funds_with_zero_available(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 1)

    # Assert
    assert (outcome.status_code, outcome.body) == (409, {"error": "insufficient_funds", "available_minor": 0})


def test_transfer_above_the_available_balance_returns_409_with_the_current_available_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 1_001)

    # Assert
    assert (outcome.status_code, outcome.body) == (
        409,
        {"error": "insufficient_funds", "available_minor": 1_000},
    )
    assert _wallet_ledger(session_factory) == {alice_wallet: (1_000, 1_000), bob_wallet: (0, 0)}
    assert _transfer_count(session_factory) == 0
    assert _journal_count(session_factory, "transfer") == 0
    assert _audit_count(session_factory) == 0


def test_transfer_does_not_spend_held_funds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _set_balance(session_factory, alice_wallet, available_minor=100, held_minor=900)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 200)

    # Assert
    assert (outcome.status_code, outcome.body) == (
        409,
        {"error": "insufficient_funds", "available_minor": 100},
    )
    assert _wallets(session_factory, alice) == [("USD", 100, 900)]


def test_a_rejected_transfer_creates_no_wallet_for_the_recipient(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "EUR", 100)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 101, currency="EUR")

    # Assert
    assert outcome.status_code == 409
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]


def test_transfer_to_an_unknown_email_returns_404_recipient_not_found_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", "nobody@example.com", 100)

    # Assert
    assert (outcome.status_code, outcome.body) == (404, {"error": "recipient_not_found"})
    assert _wallet_ledger(session_factory) == {alice_wallet: (1_000, 1_000)}
    assert _transfer_count(session_factory) == 0
    assert _audit_count(session_factory) == 0


def test_an_unknown_recipient_is_reported_before_insufficient_funds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", "nobody@example.com", 100)

    # Assert
    assert (outcome.status_code, outcome.body) == (404, {"error": "recipient_not_found"})


@pytest.mark.parametrize(
    "recipient_email",
    [ALICE, "ALICE@example.com", "Alice@Example.COM", "  alice@example.com  "],
    ids=["same", "upper_local", "mixed_case", "padded"],
)
def test_transfer_to_oneself_returns_422_self_transfer_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, recipient_email: str
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", recipient_email, 100)

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "self_transfer"})
    assert _wallet_ledger(session_factory) == {alice_wallet: (1_000, 1_000)}
    assert _transfer_count(session_factory) == 0
    assert _journal_count(session_factory, "transfer") == 0


def test_a_self_transfer_is_reported_before_insufficient_funds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", ALICE, 100)

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "self_transfer"})


# --- transfer: idempotency --------------------------------------------------------------------------------


def test_replaying_the_same_request_returns_the_stored_body_without_a_second_debit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    first = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Act
    replay = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Assert
    assert (replay.status_code, replay.body) == (201, first.body)
    assert _wallet_ledger(session_factory) == {alice_wallet: (7_500, 7_500), bob_wallet: (2_500, 2_500)}
    assert _transfer_count(session_factory) == 1
    assert _journal_count(session_factory, "transfer") == 1


def test_a_replay_with_a_differently_written_recipient_email_is_the_same_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    first = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Act
    replay = _send(session_factory, clock, alice, "k-1", "  BOB@Example.com ", 2_500)

    # Assert
    assert (replay.status_code, replay.body) == (201, first.body)
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _transfer_count(session_factory) == 1


@pytest.mark.parametrize(
    ("recipient_email", "amount_minor", "currency"),
    [(BOB, 2_501, "USD"), (CAROL, 2_500, "USD"), (BOB, 2_500, "EUR")],
    ids=["different_amount", "different_recipient", "different_currency"],
)
def test_the_same_key_with_a_different_request_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    recipient_email: str,
    amount_minor: int,
    currency: str,
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _user(session_factory, clock, CAROL)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _fund(session_factory, clock, alice, "EUR", 10_000)
    _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", recipient_email, amount_minor, currency)

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})
    assert _wallets(session_factory, alice) == [("EUR", 10_000, 0), ("USD", 7_500, 0)]
    assert _transfer_count(session_factory) == 1


def test_a_stored_key_answers_idempotency_key_reused_before_the_recipient_is_looked_up(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", "nobody@example.com", 2_500)

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})


def test_a_replay_is_answered_from_the_stored_row_even_after_the_balance_changed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _user(session_factory, clock, CAROL)
    _fund(session_factory, clock, alice, "USD", 1_000)
    first = _send(session_factory, clock, alice, "k-1", BOB, 600)
    drained = _send(session_factory, clock, alice, "k-2", CAROL, 400)

    # Act
    replay = _send(session_factory, clock, alice, "k-1", BOB, 600)

    # Assert
    assert drained.status_code == 201
    assert (replay.status_code, replay.body) == (201, first.body)
    assert replay.body["sender_available_minor"] == 400
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]
    assert _transfer_count(session_factory) == 2


def test_an_insufficient_funds_answer_is_not_stored_so_a_retry_after_a_top_up_succeeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    rejected = _send(session_factory, clock, alice, "k-1", BOB, 500)
    _fund(session_factory, clock, alice, "USD", 500)

    # Act
    retry = _send(session_factory, clock, alice, "k-1", BOB, 500)

    # Assert
    assert (rejected.status_code, rejected.body) == (
        409,
        {"error": "insufficient_funds", "available_minor": 0},
    )
    assert (retry.status_code, retry.body["amount_minor"]) == (201, 500)
    assert _wallets(session_factory, bob) == [("USD", 500, 0)]
    assert _transfer_count(session_factory) == 1


def test_a_key_that_only_ever_got_an_error_can_be_reused_for_a_different_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 100)
    rejected = _send(session_factory, clock, alice, "k-1", BOB, 500)

    # Act
    other = _send(session_factory, clock, alice, "k-1", BOB, 100)

    # Assert
    assert rejected.status_code == 409
    assert (other.status_code, other.body["amount_minor"]) == (201, 100)


def test_a_recipient_not_found_answer_is_not_stored_so_a_retry_after_they_sign_up_succeeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, "USD", 1_000)
    rejected = _send(session_factory, clock, alice, "k-1", BOB, 300)
    bob, _ = _user(session_factory, clock, BOB)

    # Act
    retry = _send(session_factory, clock, alice, "k-1", BOB, 300)

    # Assert
    assert (rejected.status_code, rejected.body) == (404, {"error": "recipient_not_found"})
    assert retry.status_code == 201
    assert _wallets(session_factory, bob) == [("USD", 300, 0)]


def test_a_replay_of_a_legacy_row_whose_stored_body_has_recipient_user_id_answers_without_that_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    legacy_id = uuid.uuid4()
    canonical = {"recipient_email": BOB, "amount_minor": 2_500, "currency": "USD"}
    row = _transfer_row(alice, bob, alice_wallet, bob_wallet, clock, key="k-legacy", amount_minor=2_500)
    row.id = legacy_id
    row.request_hash = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    row.response_body = {
        "transfer_id": str(legacy_id),
        "state": "succeeded",
        "amount_minor": 2_500,
        "currency": "USD",
        "recipient_user_id": str(bob),
        "sender_available_minor": 7_500,
    }
    with session_factory.begin() as session:
        session.add(row)

    # Act
    replay = _send(session_factory, clock, alice, "k-legacy", BOB, 2_500)

    # Assert
    assert replay.status_code == 201
    assert replay.body == {
        "transfer_id": str(legacy_id),
        "state": "succeeded",
        "amount_minor": 2_500,
        "currency": "USD",
        "sender_available_minor": 7_500,
    }
    assert _transfer_count(session_factory) == 1
    assert _journal_count(session_factory, "transfer") == 0


def test_a_self_transfer_answer_is_not_stored(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    rejected = _send(session_factory, clock, alice, "k-1", ALICE, 300)

    # Act
    other = _send(session_factory, clock, alice, "k-1", BOB, 300)

    # Assert
    assert (rejected.status_code, rejected.body) == (422, {"error": "self_transfer"})
    assert (other.status_code, other.body["amount_minor"]) == (201, 300)


def test_the_idempotency_key_is_scoped_to_the_sender(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    carol, _ = _user(session_factory, clock, CAROL)
    _fund(session_factory, clock, alice, "USD", 1_000)
    _fund(session_factory, clock, bob, "USD", 1_000)

    # Act
    from_alice = _send(session_factory, clock, alice, "shared-key", CAROL, 100)
    from_bob = _send(session_factory, clock, bob, "shared-key", CAROL, 300)

    # Assert
    assert (from_alice.status_code, from_bob.status_code) == (201, 201)
    assert _transfer_id(from_alice) != _transfer_id(from_bob)
    assert _wallets(session_factory, carol) == [("USD", 400, 0)]
    assert _transfer_count(session_factory) == 2


def test_parallel_identical_requests_under_one_key_debit_once_and_all_answer_the_same_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    jobs = [partial(_send, session_factory, clock, alice, "k-same", BOB, 300) for _ in range(PARALLEL)]

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    assert {o.status_code for o in outcomes} == {201}
    assert [o.body for o in outcomes] == [outcomes[0].body] * PARALLEL
    assert _wallet_ledger(session_factory) == {alice_wallet: (700, 700), bob_wallet: (300, 300)}
    assert _transfer_count(session_factory) == 1
    assert _journal_count(session_factory, "transfer") == 1
    assert _audit_count(session_factory) == 1


def test_parallel_identical_requests_for_the_whole_balance_all_answer_201_from_the_stored_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 500)
    jobs = [partial(_send, session_factory, clock, alice, "k-whole", BOB, 500) for _ in range(PARALLEL)]

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    assert [o.status_code for o in outcomes] == [201] * PARALLEL
    assert [o.body for o in outcomes] == [outcomes[0].body] * PARALLEL
    assert outcomes[0].body == {
        "transfer_id": str(_transfer_id(outcomes[0])),
        "state": "succeeded",
        "amount_minor": 500,
        "currency": "USD",
        "sender_available_minor": 0,
    }
    assert _transfer_count(session_factory) == 1
    assert _journal_count(session_factory, "transfer") == 1
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 500, 0)]
    assert _wallet_ledger(session_factory) == {alice_wallet: (0, 0), bob_wallet: (500, 500)}


# --- transfer: concurrency --------------------------------------------------------------------------------


def test_parallel_transfers_from_one_wallet_never_overdraw_it_and_the_sums_match(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_500)
    jobs = [partial(_send, session_factory, clock, alice, f"k-{i}", BOB, 400) for i in range(PARALLEL)]

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    rejected = [o for o in outcomes if o.status_code == 409]
    assert sorted(o.status_code for o in outcomes) == [201] * 3 + [409] * (PARALLEL - 3)
    assert {o.body["available_minor"] for o in rejected} == {300}
    assert _wallet_ledger(session_factory) == {alice_wallet: (300, 300), bob_wallet: (1_200, 1_200)}
    assert _wallets(session_factory, alice) == [("USD", 300, 0)]
    assert _wallets(session_factory, bob) == [("USD", 1_200, 0)]
    assert _transfer_count(session_factory) == 3
    assert _journal_count(session_factory, "transfer") == 3
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


def test_opposite_direction_transfers_between_the_same_pair_all_complete_without_deadlock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _fund(session_factory, clock, bob, "USD", 10_000)
    jobs: list[Callable[[], TransferOutcome]] = []
    for i in range(PARALLEL // 2 + 2):
        jobs.append(partial(_send, session_factory, clock, alice, f"a-{i}", BOB, 150))
        jobs.append(partial(_send, session_factory, clock, bob, f"b-{i}", ALICE, 100))

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    assert {o.status_code for o in outcomes} == {201}
    assert _wallet_ledger(session_factory) == {alice_wallet: (9_700, 9_700), bob_wallet: (10_300, 10_300)}
    assert _transfer_count(session_factory) == 12
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


def test_parallel_transfers_from_several_senders_to_one_recipient_create_the_new_wallet_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bob, _ = _user(session_factory, clock, BOB)
    senders = [_user(session_factory, clock, f"sender{i}@example.com")[0] for i in range(4)]
    for sender in senders:
        _fund(session_factory, clock, sender, "EUR", 1_000)
    jobs = [partial(_send, session_factory, clock, sender, "k-1", BOB, 250, "EUR") for sender in senders]

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    assert {o.status_code for o in outcomes} == {201}
    assert _wallets(session_factory, bob) == [("EUR", 1_000, 0), ("USD", 0, 0)]
    assert (
        _count(
            session_factory,
            "SELECT count(*) AS n FROM gateway.ledger_accounts "
            "WHERE owner_id = :id AND kind = 'wallet' AND currency = 'EUR'",
            id=bob,
        )
        == 1
    )
    assert _ledger_sum_by_currency(session_factory) == {"EUR": 0}


# --- transfer: audit --------------------------------------------------------------------------------------


def test_a_created_transfer_writes_one_audit_row_without_any_email(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log "
        "WHERE action LIKE 'transfer.%' ORDER BY id",
    )
    assert rows == [
        {
            "actor_id": f"user:{alice}",
            "actor_role": "consumer",
            "action": "transfer.created",
            "target": f"transfer:{_transfer_id(outcome)}",
            "summary": {"amount_minor": 2_500, "currency": "USD", "recipient_user_id": str(bob)},
        }
    ]
    assert "@" not in str(rows)


def test_a_replay_writes_no_second_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Act
    _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Assert
    assert _audit_count(session_factory) == 1


def test_a_rejected_key_reuse_writes_no_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _send(session_factory, clock, alice, "k-1", BOB, 2_500)

    # Act
    _send(session_factory, clock, alice, "k-1", BOB, 2_501)

    # Assert
    assert _audit_count(session_factory) == 1


# --- top_up_wallet ----------------------------------------------------------------------------------------


def test_top_up_wallet_credits_the_signup_wallet_and_returns_its_account_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)

    # Act
    wallet_id = _fund(session_factory, clock, alice, "USD", 4_200)

    # Assert
    assert wallet_id == alice_wallet
    assert _wallets(session_factory, alice) == [("USD", 4_200, 0)]
    assert _wallet_ledger(session_factory) == {alice_wallet: (4_200, 4_200)}


def test_top_up_wallet_posts_a_balanced_top_up_journal_against_the_platform_topup_clearing_account(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)

    # Act
    _fund(session_factory, clock, alice, "USD", 4_200)

    # Assert
    lines = _rows(
        session_factory,
        "SELECT j.ref_type, la.owner_type, la.owner_id, la.kind, la.id AS account_id, p.currency, "
        "p.amount_minor FROM gateway.journal_entries j "
        "JOIN gateway.postings p ON p.journal_id = j.id "
        "JOIN gateway.ledger_accounts la ON la.id = p.account_id "
        "WHERE j.ref_type = 'top_up' ORDER BY p.amount_minor",
    )
    assert _journal_count(session_factory, "top_up") == 1
    assert lines == [
        {
            "ref_type": "top_up",
            "owner_type": "user",
            "owner_id": alice,
            "kind": "wallet",
            "account_id": alice_wallet,
            "currency": "USD",
            "amount_minor": -4_200,
        },
        {
            "ref_type": "top_up",
            "owner_type": "platform",
            "owner_id": None,
            "kind": "topup_clearing",
            "account_id": lines[1]["account_id"],
            "currency": "USD",
            "amount_minor": 4_200,
        },
    ]
    assert _ledger_sum_by_currency(session_factory) == {"USD": 0}


def test_top_up_wallet_creates_the_wallet_when_the_user_has_none_in_that_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_usd_wallet = _user(session_factory, clock, ALICE)

    # Act
    wallet_id = _fund(session_factory, clock, alice, "INR", 8_000)

    # Assert
    with session_factory() as session:
        views = [
            (v.account_id, v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, alice)
        ]
    assert views == [(wallet_id, "INR", 8_000, 0), (alice_usd_wallet, "USD", 0, 0)]
    assert _wallet_ledger(session_factory)[wallet_id] == (8_000, 8_000)
    assert _ledger_sum_by_currency(session_factory) == {"INR": 0}


def test_top_up_wallet_adds_to_the_balance_on_every_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)

    # Act
    first = _fund(session_factory, clock, alice, "USD", 1_000)
    second = _fund(session_factory, clock, alice, "USD", 2_500)

    # Assert
    assert (first, second) == (alice_wallet, alice_wallet)
    assert _wallet_ledger(session_factory) == {alice_wallet: (3_500, 3_500)}
    assert _journal_count(session_factory, "top_up") == 2


@pytest.mark.parametrize("amount_minor", [0, -1, -5_000])
def test_top_up_wallet_rejects_a_non_positive_amount_with_value_error_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, amount_minor: int
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)

    # Act
    with pytest.raises(ValueError):
        with session_factory.begin() as session:
            top_up_wallet(session, clock, alice, "USD", amount_minor)

    # Assert
    assert _wallet_ledger(session_factory) == {alice_wallet: (0, 0)}
    assert _journal_count(session_factory, "top_up") == 0


def test_top_up_wallet_does_not_commit_the_caller_session(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)

    # Act
    with session_factory() as session:
        top_up_wallet(session, clock, alice, "USD", 900)
        session.rollback()

    # Assert
    assert _wallet_ledger(session_factory) == {alice_wallet: (0, 0)}
    assert _journal_count(session_factory, "top_up") == 0


# --- ensure_wallet ----------------------------------------------------------------------------------------


def test_ensure_wallet_creates_a_user_wallet_account_and_a_zero_balance_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_usd_wallet = _user(session_factory, clock, ALICE)

    # Act
    with session_factory.begin() as session:
        wallet_id = ensure_wallet(session, alice, "EUR")

    # Assert
    with session_factory() as session:
        views = [
            (v.account_id, v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, alice)
        ]
    assert views == [(wallet_id, "EUR", 0, 0), (alice_usd_wallet, "USD", 0, 0)]


def test_ensure_wallet_returns_the_existing_wallet_without_touching_its_balance(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, "USD", 3_000)

    # Act
    with session_factory.begin() as session:
        first = ensure_wallet(session, alice, "USD")
        second = ensure_wallet(session, alice, "USD")

    # Assert
    assert (first, second) == (alice_wallet, alice_wallet)
    assert _wallets(session_factory, alice) == [("USD", 3_000, 0)]


def test_ensure_wallet_gives_different_users_different_wallets_in_the_same_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)

    # Act
    with session_factory.begin() as session:
        alice_wallet = ensure_wallet(session, alice, "JPY")
        bob_wallet = ensure_wallet(session, bob, "JPY")

    # Assert
    assert alice_wallet != bob_wallet
    assert _wallets(session_factory, alice) == [("JPY", 0, 0), ("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("JPY", 0, 0), ("USD", 0, 0)]


def test_parallel_ensure_wallet_calls_create_exactly_one_account_and_balance_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)

    def ensure() -> uuid.UUID:
        with session_factory.begin() as session:
            return ensure_wallet(session, alice, "EUR")

    # Act
    wallet_ids = _run_parallel([ensure for _ in range(PARALLEL)])

    # Assert
    assert len(set(wallet_ids)) == 1
    assert (
        _count(
            session_factory,
            "SELECT count(*) AS n FROM gateway.ledger_accounts "
            "WHERE owner_id = :id AND kind = 'wallet' AND currency = 'EUR'",
            id=alice,
        )
        == 1
    )
    assert (
        _count(
            session_factory,
            "SELECT count(*) AS n FROM gateway.account_balances WHERE account_id = :id",
            id=wallet_ids[0],
        )
        == 1
    )


# --- Transfer model ---------------------------------------------------------------------------------------


def _transfer_row(
    sender: uuid.UUID,
    recipient: uuid.UUID,
    sender_account: uuid.UUID,
    recipient_account: uuid.UUID,
    clock: FixedClock,
    key: str = "k-1",
    amount_minor: int = 100,
) -> Transfer:
    return Transfer(
        id=uuid.uuid4(),
        sender_user_id=sender,
        recipient_user_id=recipient,
        sender_account_id=sender_account,
        recipient_account_id=recipient_account,
        amount_minor=amount_minor,
        currency="USD",
        state="succeeded",
        idempotency_key=key,
        request_hash="0" * 64,
        response_body={"state": "succeeded"},
        created_at=clock(),
    )


def test_transfer_model_round_trips_every_column(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    row = _transfer_row(alice, bob, alice_wallet, bob_wallet, clock)

    # Act
    with session_factory.begin() as session:
        session.add(row)
    with session_factory() as session:
        loaded = session.scalars(select(Transfer)).one()
        values = (
            loaded.id,
            loaded.sender_user_id,
            loaded.recipient_user_id,
            loaded.sender_account_id,
            loaded.recipient_account_id,
            loaded.amount_minor,
            loaded.currency,
            loaded.state,
            loaded.idempotency_key,
            loaded.request_hash,
            loaded.response_body,
            loaded.created_at,
        )

    # Assert
    assert values == (
        row.id,
        alice,
        bob,
        alice_wallet,
        bob_wallet,
        100,
        "USD",
        "succeeded",
        "k-1",
        "0" * 64,
        {"state": "succeeded"},
        clock(),
    )


@pytest.mark.parametrize("amount_minor", [0, -1])
def test_transfer_model_rejects_a_non_positive_amount_with_the_amount_positive_check(
    session_factory: sessionmaker[Session], clock: FixedClock, amount_minor: int
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    row = _transfer_row(alice, bob, alice_wallet, bob_wallet, clock, amount_minor=amount_minor)

    # Act
    with pytest.raises(IntegrityError) as excinfo:
        with session_factory.begin() as session:
            session.add(row)

    # Assert
    assert "amount_positive" in str(excinfo.value)
    assert _transfer_count(session_factory) == 0


def test_transfer_model_enforces_a_unique_idempotency_key_per_sender(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)
    with session_factory.begin() as session:
        session.add(_transfer_row(alice, bob, alice_wallet, bob_wallet, clock))

    # Act
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            session.add(_transfer_row(alice, bob, alice_wallet, bob_wallet, clock))

    # Assert
    assert _transfer_count(session_factory) == 1


def test_transfer_model_allows_the_same_idempotency_key_for_a_different_sender(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, alice_wallet = _user(session_factory, clock, ALICE)
    bob, bob_wallet = _user(session_factory, clock, BOB)

    # Act
    with session_factory.begin() as session:
        session.add(_transfer_row(alice, bob, alice_wallet, bob_wallet, clock))
        session.add(_transfer_row(bob, alice, bob_wallet, alice_wallet, clock))

    # Assert
    assert _transfer_count(session_factory) == 2
