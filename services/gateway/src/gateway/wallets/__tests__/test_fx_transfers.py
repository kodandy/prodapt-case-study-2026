"""Tests for cross-currency `transfer(..., recipient_currency=...)`, written from docs/specs/gateway-basic.md
section "B1.4 / B2.3 / B2.5 / P0-2 / P0-3" (Decisions 2-5, 9, and the `list_transfers` note).

Needs the 12 migration-seeded demo rates (`gateway.fx_rates`) to survive the per-test table reset.
"""

import hashlib
import json
import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal

import pytest
from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from gateway.wallets.history import list_transfers
from gateway.wallets.service import (
    TransferOutcome,
    ledger_wallet_balances,
    list_wallets,
    top_up_wallet,
    transfer,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"
ALICE = "alice@example.com"
BOB = "bob@example.com"
EXPONENTS = {"USD": 2, "EUR": 2, "JPY": 0, "INR": 2}
PARALLEL = 6


def _user(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _fund(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, currency: str, amount: int
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, currency, amount)


def _send(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender: uuid.UUID,
    key: str,
    recipient_email: str,
    amount_minor: int,
    currency: str,
    recipient_currency: str | None,
) -> TransferOutcome:
    return transfer(
        session_factory,
        clock,
        sender_user_id=sender,
        idempotency_key=key,
        recipient_email=recipient_email,
        amount_minor=amount_minor,
        currency=currency,
        recipient_currency=recipient_currency,
    )


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str, **params: object) -> int:
    return int(str(_rows(session_factory, query, **params)[0]["n"]))


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _rate(session_factory: sessionmaker[Session], rate_id: str) -> Decimal:
    rows = _rows(session_factory, "SELECT rate FROM gateway.fx_rates WHERE id = :id", id=rate_id)
    return Decimal(str(rows[0]["rate"]))


def _expected_target(rate: Decimal, amount_minor: int, source: str, target: str) -> int:
    shift = Decimal(10) ** (EXPONENTS[target] - EXPONENTS[source])
    return int((Decimal(amount_minor) * rate * shift).to_integral_value(rounding=ROUND_FLOOR))


def _transfer_id(outcome: TransferOutcome) -> uuid.UUID:
    return uuid.UUID(str(outcome.body["transfer_id"]))


def _transfer_journal(
    session_factory: sessionmaker[Session], transfer_id: uuid.UUID
) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT j.ref_type, j.fx_rate_id, p.currency, p.amount_minor, a.owner_type, a.owner_id, a.kind "
        "FROM gateway.journal_entries j JOIN gateway.postings p ON p.journal_id = j.id "
        "JOIN gateway.ledger_accounts a ON a.id = p.account_id "
        "WHERE j.ref_id = :ref_id AND j.ref_type = 'transfer' ORDER BY p.currency, p.amount_minor",
        ref_id=transfer_id,
    )


def _transfer_journal_count(session_factory: sessionmaker[Session]) -> int:
    return _count(
        session_factory, "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = 'transfer'"
    )


def _transfer_row_count(session_factory: sessionmaker[Session]) -> int:
    return _count(session_factory, "SELECT count(*) AS n FROM gateway.transfers")


def _transfer_audit_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log "
        "WHERE action LIKE 'transfer.%' ORDER BY id",
    )


def _insert_rate(
    session_factory: sessionmaker[Session],
    rate_id: str,
    base: str,
    quoted: str,
    rate: str,
    valid_from: datetime,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO gateway.fx_rates (id, base_currency, quote_currency, rate, valid_from) "
                "VALUES (:id, :base, :quote, :rate, :valid_from)"
            ),
            {"id": rate_id, "base": base, "quote": quoted, "rate": Decimal(rate), "valid_from": valid_from},
        )


def _legacy_hash(email: str, amount_minor: int, currency: str) -> str:
    canonical = {"recipient_email": email, "amount_minor": amount_minor, "currency": currency}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _run_parallel[T](jobs: Sequence[Callable[[], T]]) -> list[T]:
    barrier = threading.Barrier(len(jobs))

    def run(job: Callable[[], T]) -> T:
        barrier.wait(timeout=30)
        return job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [future.result(timeout=60) for future in futures]


# --- cross-currency transfer: happy path ---------------------------------------------------------------


def test_cross_currency_transfer_debits_the_sender_and_credits_the_converted_amount(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-1", BOB, 10_000, "USD", "EUR")

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == {
        "transfer_id": str(_transfer_id(outcome)),
        "state": "succeeded",
        "amount_minor": 10_000,
        "currency": "USD",
        "sender_available_minor": 15_000,
        "recipient_currency": "EUR",
        "recipient_amount_minor": 9_200,
        "fx_rate_id": "demo-USDEUR-v1",
    }
    assert _wallets(session_factory, alice) == [("USD", 15_000, 0)]
    assert _wallets(session_factory, bob) == [("EUR", 9_200, 0), ("USD", 0, 0)]


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("USD", "EUR"),
        ("USD", "JPY"),
        ("USD", "INR"),
        ("EUR", "USD"),
        ("JPY", "USD"),
        ("JPY", "INR"),
        ("INR", "JPY"),
        ("EUR", "INR"),
    ],
)
def test_cross_currency_transfer_credits_floor_of_the_seeded_rate_for_every_pair(
    session_factory: sessionmaker[Session], clock: FixedClock, source: str, target: str
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, source, 1_000_000)
    rate_id = f"demo-{source}{target}-v1"
    expected = _expected_target(_rate(session_factory, rate_id), 123_457, source, target)

    # Act
    outcome = _send(session_factory, clock, alice, "k-pair", BOB, 123_457, source, target)

    # Assert
    assert expected > 0
    assert outcome.status_code == 201
    assert outcome.body["recipient_amount_minor"] == expected
    assert outcome.body["fx_rate_id"] == rate_id
    assert (target, expected, 0) in _wallets(session_factory, bob)
    assert (source, 1_000_000 - 123_457, 0) in _wallets(session_factory, alice)


def test_cross_currency_transfer_into_jpy_uses_zero_decimal_minor_units(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-jpy", BOB, 10_000, "USD", "JPY")

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["recipient_amount_minor"] == 15_150
    assert _wallets(session_factory, bob) == [("JPY", 15_150, 0), ("USD", 0, 0)]


def test_cross_currency_transfer_out_of_jpy_scales_up_to_two_decimal_minor_units(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "JPY", 100_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-from-jpy", BOB, 100_000, "JPY", "USD")

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["recipient_amount_minor"] == 66_006
    assert _wallets(session_factory, alice) == [("JPY", 0, 0), ("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 66_006, 0)]


def test_cross_currency_transfer_credits_an_existing_recipient_wallet_in_the_target_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    _fund(session_factory, clock, bob, "EUR", 500)

    # Act
    outcome = _send(session_factory, clock, alice, "k-existing", BOB, 10_000, "USD", "EUR")

    # Assert
    assert outcome.status_code == 201
    assert _wallets(session_factory, bob) == [("EUR", 9_700, 0), ("USD", 0, 0)]


def test_cross_currency_transfer_keeps_wallet_balances_equal_to_the_postings(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    _send(session_factory, clock, alice, "k-ledger", BOB, 10_000, "USD", "EUR")

    # Assert
    with session_factory() as session:
        assert ledger_wallet_balances(session, alice) == list_wallets(session, alice)
        assert ledger_wallet_balances(session, bob) == list_wallets(session, bob)


# --- journal and transfer row --------------------------------------------------------------------------


def test_cross_currency_transfer_posts_one_four_line_fx_journal_with_the_rate_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-journal", BOB, 10_000, "USD", "EUR")

    # Assert
    lines = _transfer_journal(session_factory, _transfer_id(outcome))
    assert _transfer_journal_count(session_factory) == 1
    assert lines == [
        {
            "ref_type": "transfer",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "EUR",
            "amount_minor": -9_200,
            "owner_type": "user",
            "owner_id": bob,
            "kind": "wallet",
        },
        {
            "ref_type": "transfer",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "EUR",
            "amount_minor": 9_200,
            "owner_type": "platform",
            "owner_id": None,
            "kind": "fx_clearing",
        },
        {
            "ref_type": "transfer",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "USD",
            "amount_minor": -10_000,
            "owner_type": "platform",
            "owner_id": None,
            "kind": "fx_clearing",
        },
        {
            "ref_type": "transfer",
            "fx_rate_id": "demo-USDEUR-v1",
            "currency": "USD",
            "amount_minor": 10_000,
            "owner_type": "user",
            "owner_id": alice,
            "kind": "wallet",
        },
    ]


def test_cross_currency_transfer_leaves_every_currency_netting_to_zero_in_the_ledger(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    _send(session_factory, clock, alice, "k-net", BOB, 10_001, "USD", "EUR")

    # Assert
    totals = _rows(
        session_factory,
        "SELECT currency, sum(amount_minor)::bigint AS total FROM gateway.postings GROUP BY currency "
        "ORDER BY currency",
    )
    assert totals == [{"currency": "EUR", "total": 0}, {"currency": "USD", "total": 0}]


def test_cross_currency_transfer_stores_the_conversion_on_the_transfer_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-row", BOB, 10_000, "USD", "EUR")

    # Assert
    rows = _rows(
        session_factory,
        "SELECT id, sender_user_id, recipient_user_id, amount_minor, currency, recipient_currency, "
        "recipient_amount_minor, fx_rate_id, state FROM gateway.transfers",
    )
    assert rows == [
        {
            "id": _transfer_id(outcome),
            "sender_user_id": alice,
            "recipient_user_id": bob,
            "amount_minor": 10_000,
            "currency": "USD",
            "recipient_currency": "EUR",
            "recipient_amount_minor": 9_200,
            "fx_rate_id": "demo-USDEUR-v1",
            "state": "succeeded",
        }
    ]


def test_cross_currency_transfer_uses_the_rate_in_force_at_the_clock_time(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)
    _insert_rate(session_factory, "test-USDEUR-v2", "USD", "EUR", "0.90", datetime(2026, 11, 1, tzinfo=UTC))

    # Act
    outcome = _send(session_factory, clock, alice, "k-v2", BOB, 10_000, "USD", "EUR")

    # Assert
    assert outcome.body["fx_rate_id"] == "test-USDEUR-v2"
    assert outcome.body["recipient_amount_minor"] == 9_000
    assert _transfer_journal(session_factory, _transfer_id(outcome))[0]["fx_rate_id"] == "test-USDEUR-v2"


# --- same currency stays byte-for-byte as in B2 --------------------------------------------------------


@pytest.mark.parametrize("recipient_currency", [None, "USD"], ids=["absent", "equal-to-currency"])
def test_same_currency_transfer_body_is_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock, recipient_currency: str | None
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-same", BOB, 2_500, "USD", recipient_currency)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == {
        "transfer_id": str(_transfer_id(outcome)),
        "state": "succeeded",
        "amount_minor": 2_500,
        "currency": "USD",
        "sender_available_minor": 7_500,
    }
    assert _wallets(session_factory, bob) == [("USD", 2_500, 0)]


@pytest.mark.parametrize("recipient_currency", [None, "USD"], ids=["absent", "equal-to-currency"])
def test_same_currency_transfer_posts_a_two_line_journal_without_an_fx_rate(
    session_factory: sessionmaker[Session], clock: FixedClock, recipient_currency: str | None
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-same-j", BOB, 2_500, "USD", recipient_currency)

    # Assert
    lines = _transfer_journal(session_factory, _transfer_id(outcome))
    assert lines == [
        {
            "ref_type": "transfer",
            "fx_rate_id": None,
            "currency": "USD",
            "amount_minor": -2_500,
            "owner_type": "user",
            "owner_id": bob,
            "kind": "wallet",
        },
        {
            "ref_type": "transfer",
            "fx_rate_id": None,
            "currency": "USD",
            "amount_minor": 2_500,
            "owner_type": "user",
            "owner_id": alice,
            "kind": "wallet",
        },
    ]


@pytest.mark.parametrize("recipient_currency", [None, "USD"], ids=["absent", "equal-to-currency"])
def test_same_currency_transfer_row_has_no_fx_columns_and_the_b2_request_hash(
    session_factory: sessionmaker[Session], clock: FixedClock, recipient_currency: str | None
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    _send(session_factory, clock, alice, "k-same-r", BOB, 2_500, "USD", recipient_currency)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT recipient_currency, recipient_amount_minor, fx_rate_id, request_hash FROM gateway.transfers",
    )
    assert rows == [
        {
            "recipient_currency": None,
            "recipient_amount_minor": None,
            "fx_rate_id": None,
            "request_hash": _legacy_hash(BOB, 2_500, "USD"),
        }
    ]


@pytest.mark.parametrize("recipient_currency", [None, "USD"], ids=["absent", "equal-to-currency"])
def test_same_currency_transfer_audit_summary_keeps_the_three_b2_keys(
    session_factory: sessionmaker[Session], clock: FixedClock, recipient_currency: str | None
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-same-a", BOB, 2_500, "USD", recipient_currency)

    # Assert
    assert _transfer_audit_rows(session_factory) == [
        {
            "actor_id": f"user:{alice}",
            "actor_role": "consumer",
            "action": "transfer.created",
            "target": f"transfer:{_transfer_id(outcome)}",
            "summary": {"amount_minor": 2_500, "currency": "USD", "recipient_user_id": str(bob)},
        }
    ]


def test_same_key_with_recipient_currency_absent_then_equal_is_the_same_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    first = _send(session_factory, clock, alice, "k-same-eq", BOB, 2_500, "USD", None)

    # Act
    second = _send(session_factory, clock, alice, "k-same-eq", BOB, 2_500, "USD", "USD")

    # Assert
    assert second == first
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _transfer_row_count(session_factory) == 1


# --- audit ---------------------------------------------------------------------------------------------


def test_cross_currency_transfer_audit_summary_adds_the_three_fx_keys(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-audit", BOB, 10_000, "USD", "EUR")

    # Assert
    assert _transfer_audit_rows(session_factory) == [
        {
            "actor_id": f"user:{alice}",
            "actor_role": "consumer",
            "action": "transfer.created",
            "target": f"transfer:{_transfer_id(outcome)}",
            "summary": {
                "amount_minor": 10_000,
                "currency": "USD",
                "recipient_user_id": str(bob),
                "recipient_currency": "EUR",
                "recipient_amount_minor": 9_200,
                "fx_rate_id": "demo-USDEUR-v1",
            },
        }
    ]


# --- insufficient funds and FX errors move nothing -----------------------------------------------------


def test_cross_currency_transfer_with_insufficient_funds_answers_409_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 5_000)

    # Act
    outcome = _send(session_factory, clock, alice, "k-poor", BOB, 10_000, "USD", "EUR")

    # Assert
    assert (outcome.status_code, outcome.body) == (
        409,
        {"error": "insufficient_funds", "available_minor": 5_000},
    )
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]
    assert _transfer_row_count(session_factory) == 0
    assert _transfer_journal_count(session_factory) == 0
    assert _transfer_audit_rows(session_factory) == []


def test_cross_currency_transfer_from_a_currency_without_a_wallet_answers_409_with_zero_available(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)

    # Act
    outcome = _send(session_factory, clock, alice, "k-nowallet", BOB, 10_000, "EUR", "USD")

    # Assert
    assert (outcome.status_code, outcome.body) == (409, {"error": "insufficient_funds", "available_minor": 0})
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]
    assert _transfer_row_count(session_factory) == 0


def test_cross_currency_transfer_whose_converted_amount_is_zero_answers_422_before_any_lock_or_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: 1 JPY * 0.0066006600 * 100 = 0.66 USD minor units -> 0
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "JPY", 100)

    # Act
    outcome = _send(session_factory, clock, alice, "k-small", BOB, 1, "JPY", "USD")

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "fx_amount_too_small"})
    assert _wallets(session_factory, alice) == [("JPY", 100, 0), ("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]
    assert _transfer_row_count(session_factory) == 0
    assert _transfer_journal_count(session_factory) == 0


def test_cross_currency_transfer_too_small_does_not_create_the_recipient_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: 1 JPY * (0.92 / 151.5) * 100 = 0.60 EUR minor units -> 0
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "JPY", 100)

    # Act
    outcome = _send(session_factory, clock, alice, "k-small-eur", BOB, 1, "JPY", "EUR")

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "fx_amount_too_small"})
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]


def test_cross_currency_transfer_without_a_rate_in_force_answers_422_fx_rate_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the seeded rates are valid from 2026-01-01
    before_rates = FixedClock(datetime(2025, 12, 31, 12, 0, tzinfo=UTC))
    alice = _user(session_factory, before_rates, ALICE)
    bob = _user(session_factory, before_rates, BOB)
    _fund(session_factory, before_rates, alice, "USD", 10_000)

    # Act
    outcome = _send(session_factory, before_rates, alice, "k-norate", BOB, 10_000, "USD", "EUR")

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "fx_rate_unavailable"})
    assert _wallets(session_factory, alice) == [("USD", 10_000, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]
    assert _transfer_row_count(session_factory) == 0
    assert _transfer_journal_count(session_factory) == 0


def test_a_failed_cross_currency_transfer_does_not_burn_the_idempotency_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 5_000)
    failed = _send(session_factory, clock, alice, "k-retry", BOB, 10_000, "USD", "EUR")
    _fund(session_factory, clock, alice, "USD", 5_000)

    # Act
    retried = _send(session_factory, clock, alice, "k-retry", BOB, 10_000, "USD", "EUR")

    # Assert
    assert failed.status_code == 409
    assert retried.status_code == 201
    assert retried.body["recipient_amount_minor"] == 9_200
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]


# --- idempotency ---------------------------------------------------------------------------------------


def test_cross_currency_replay_returns_the_stored_body_and_moves_nothing_more(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)
    first = _send(session_factory, clock, alice, "k-replay", BOB, 10_000, "USD", "EUR")

    # Act
    second = _send(session_factory, clock, alice, "k-replay", BOB, 10_000, "USD", "EUR")

    # Assert
    assert second == first
    assert _wallets(session_factory, alice) == [("USD", 15_000, 0)]
    assert _wallets(session_factory, bob) == [("EUR", 9_200, 0), ("USD", 0, 0)]
    assert _transfer_row_count(session_factory) == 1
    assert _transfer_journal_count(session_factory) == 1
    assert len(_transfer_audit_rows(session_factory)) == 1


def test_cross_currency_replay_keeps_the_original_rate_after_a_newer_rate_appears(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)
    first = _send(session_factory, clock, alice, "k-replay-rate", BOB, 10_000, "USD", "EUR")
    _insert_rate(session_factory, "test-USDEUR-v2", "USD", "EUR", "0.50", datetime(2026, 11, 1, tzinfo=UTC))

    # Act
    second = _send(session_factory, clock, alice, "k-replay-rate", BOB, 10_000, "USD", "EUR")

    # Assert
    assert second == first
    assert second.body["fx_rate_id"] == "demo-USDEUR-v1"
    assert second.body["recipient_amount_minor"] == 9_200


def test_the_same_key_with_a_different_recipient_currency_is_a_reused_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)
    _send(session_factory, clock, alice, "k-reuse", BOB, 10_000, "USD", "EUR")

    # Act
    outcome = _send(session_factory, clock, alice, "k-reuse", BOB, 10_000, "USD", "JPY")

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})
    assert _wallets(session_factory, alice) == [("USD", 15_000, 0)]
    assert _wallets(session_factory, bob) == [("EUR", 9_200, 0), ("USD", 0, 0)]


def test_the_same_key_cross_currency_then_same_currency_is_a_reused_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)
    _send(session_factory, clock, alice, "k-reuse-2", BOB, 10_000, "USD", "EUR")

    # Act
    outcome = _send(session_factory, clock, alice, "k-reuse-2", BOB, 10_000, "USD", None)

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})
    assert _transfer_row_count(session_factory) == 1


def test_cross_currency_request_hash_differs_from_the_b2_hash_and_between_recipient_currencies(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)

    # Act
    _send(session_factory, clock, alice, "k-h-eur", BOB, 2_500, "USD", "EUR")
    _send(session_factory, clock, alice, "k-h-jpy", BOB, 2_500, "USD", "JPY")

    # Assert
    hashes = [
        str(row["request_hash"])
        for row in _rows(
            session_factory, "SELECT request_hash FROM gateway.transfers ORDER BY idempotency_key"
        )
    ]
    assert len(hashes) == 2
    assert len(set(hashes)) == 2
    assert _legacy_hash(BOB, 2_500, "USD") not in hashes


# --- history -------------------------------------------------------------------------------------------


def test_history_shows_the_credited_amount_and_currency_on_the_received_side(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 25_000)
    outcome = _send(session_factory, clock, alice, "k-hist", BOB, 10_000, "USD", "EUR")

    # Act
    with session_factory() as session:
        received = list_transfers(session, bob).items
        sent = list_transfers(session, alice).items

    # Assert
    assert [
        (item.transfer_id, item.direction, item.counterparty_email_masked, item.amount_minor, item.currency)
        for item in received
    ] == [(_transfer_id(outcome), "received", "a***@example.com", 9_200, "EUR")]
    assert [
        (item.transfer_id, item.direction, item.counterparty_email_masked, item.amount_minor, item.currency)
        for item in sent
    ] == [(_transfer_id(outcome), "sent", "b***@example.com", 10_000, "USD")]


def test_history_of_a_same_currency_transfer_is_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    outcome = _send(session_factory, clock, alice, "k-hist-same", BOB, 2_500, "USD", None)

    # Act
    with session_factory() as session:
        received = list_transfers(session, bob).items

    # Assert
    assert [(item.transfer_id, item.direction, item.amount_minor, item.currency) for item in received] == [
        (_transfer_id(outcome), "received", 2_500, "USD")
    ]


# --- concurrency (P0-3) --------------------------------------------------------------------------------


def test_parallel_cross_currency_transfers_from_one_wallet_never_overdraw_it(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: funds for exactly three of the six 3_000 transfers
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    jobs = [
        (lambda n=n: _send(session_factory, clock, alice, f"k-par-{n}", BOB, 3_000, "USD", "EUR"))
        for n in range(PARALLEL)
    ]

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    statuses = sorted(outcome.status_code for outcome in outcomes)
    assert statuses == [201, 201, 201, 409, 409, 409]
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _wallets(session_factory, bob) == [("EUR", 3 * 2_760, 0), ("USD", 0, 0)]
    assert _transfer_journal_count(session_factory) == 3


def test_opposite_direction_cross_currency_transfers_complete_without_deadlock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000_000)
    _fund(session_factory, clock, bob, "EUR", 1_000_000)
    eur_usd = _expected_target(_rate(session_factory, "demo-EURUSD-v1"), 1_000, "EUR", "USD")
    jobs: list[Callable[[], TransferOutcome]] = []
    for n in range(PARALLEL):
        jobs.append(lambda n=n: _send(session_factory, clock, alice, f"k-a-{n}", BOB, 1_000, "USD", "EUR"))
        jobs.append(lambda n=n: _send(session_factory, clock, bob, f"k-b-{n}", ALICE, 1_000, "EUR", "USD"))

    # Act
    outcomes = _run_parallel(jobs)

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * (2 * PARALLEL)
    assert _wallets(session_factory, alice) == [("USD", 1_000_000 - PARALLEL * 1_000 + PARALLEL * eur_usd, 0)]
    assert _wallets(session_factory, bob) == [
        ("EUR", 1_000_000 - PARALLEL * 1_000 + PARALLEL * 920, 0),
        ("USD", 0, 0),
    ]
