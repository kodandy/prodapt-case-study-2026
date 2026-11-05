"""Tests for `create_wallet`, `ledger_wallet_balances` and `top_up`, written from docs/specs/gateway-basic.md
section B1.4 / B2.3 / B2.5 / P0-2 / P0-3 (Decisions 1, 7, 8 and `gateway/wallets/service.py (additions)`).

`RailResult` is imported from the `gateway.payments.rails` package, so its `__init__` must re-export it.
"""

import functools
import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

import pytest
from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from gateway.payments.ledger.service import ensure_account
from gateway.payments.rails import RailResult
from gateway.wallets.models import AccountBalance
from gateway.wallets.payouts import PayoutDeps, create_payout
from gateway.wallets.service import (
    TopUpOutcome,
    create_wallet,
    ledger_wallet_balances,
    list_wallets,
    top_up,
    top_up_wallet,
    transfer,
)
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"
ALICE = "alice@example.com"
BOB = "bob@example.com"
PARALLEL = 8
DESTINATION = "DE89370400440532013000"


class _FakeRail:
    name = "fake"

    def __init__(self, result: RailResult) -> None:
        self._result = result

    def submit(
        self, payout_id: str, method: str, amount_minor: int, currency: str, destination: str
    ) -> RailResult:
        return self._result


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
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, currency, amount_minor)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str, **params: object) -> int:
    return int(str(_rows(session_factory, query, **params)[0]["n"]))


def _views(
    session_factory: sessionmaker[Session], user_id: uuid.UUID, ledger: bool = False
) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        found = ledger_wallet_balances(session, user_id) if ledger else list_wallets(session, user_id)
        return [(v.currency, v.available_minor, v.held_minor) for v in found]


def _top_up(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    key: str,
    amount_minor: int,
    currency: str = "USD",
) -> TopUpOutcome:
    return top_up(session_factory, clock, user_id, key, currency, amount_minor)


def _top_up_id(body: dict[str, object]) -> uuid.UUID:
    return uuid.UUID(str(body["top_up_id"]))


def _run_parallel[T](jobs: Sequence[Callable[[], T]]) -> list[T]:
    barrier = threading.Barrier(len(jobs))

    def run(job: Callable[[], T]) -> T:
        barrier.wait(timeout=30)
        return job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [future.result(timeout=30) for future in futures]


# --- create_wallet ----------------------------------------------------------------------------------------


def test_create_wallet_creates_a_zero_balance_wallet_in_a_new_currency_and_reports_created(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    with session_factory.begin() as session:
        view, created = create_wallet(session, user_id, "EUR")

    # Assert
    assert created is True
    assert (view.currency, view.available_minor, view.held_minor) == ("EUR", 0, 0)
    assert _views(session_factory, user_id) == [("EUR", 0, 0), ("USD", 0, 0)]
    with session_factory() as session:
        listed = {v.currency: v.account_id for v in list_wallets(session, user_id)}
    assert view.account_id == listed["EUR"]


def test_create_wallet_stores_a_zero_balance_row_for_the_new_account(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    with session_factory.begin() as session:
        view, _ = create_wallet(session, user_id, "JPY")

    # Assert
    rows = _rows(
        session_factory,
        "SELECT available_minor, held_minor FROM gateway.account_balances WHERE account_id = :id",
        id=view.account_id,
    )
    assert rows == [{"available_minor": 0, "held_minor": 0}]


def test_create_wallet_for_an_existing_currency_returns_the_wallet_and_false(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, ALICE)

    # Act
    with session_factory.begin() as session:
        view, created = create_wallet(session, user_id, "USD")

    # Assert
    assert created is False
    assert (view.account_id, view.currency, view.available_minor, view.held_minor) == (wallet_id, "USD", 0, 0)
    assert _views(session_factory, user_id) == [("USD", 0, 0)]


def test_create_wallet_for_an_existing_funded_wallet_returns_its_current_balance(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, user_id, "USD", 4_200)

    # Act
    with session_factory.begin() as session:
        view, created = create_wallet(session, user_id, "USD")

    # Assert
    assert created is False
    assert (view.currency, view.available_minor, view.held_minor) == ("USD", 4_200, 0)


def test_create_wallet_twice_creates_once_and_returns_the_same_account(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    with session_factory.begin() as session:
        first_view, first_created = create_wallet(session, user_id, "INR")
    with session_factory.begin() as session:
        second_view, second_created = create_wallet(session, user_id, "INR")

    # Assert
    assert (first_created, second_created) == (True, False)
    assert second_view == first_view
    assert _views(session_factory, user_id) == [("INR", 0, 0), ("USD", 0, 0)]


def test_create_wallet_posts_no_journal(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    with session_factory.begin() as session:
        create_wallet(session, user_id, "EUR")

    # Assert
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries") == 0


def test_create_wallet_does_not_touch_another_users_wallets(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)

    # Act
    with session_factory.begin() as session:
        create_wallet(session, alice, "EUR")

    # Assert
    assert _views(session_factory, alice) == [("EUR", 0, 0), ("USD", 0, 0)]
    assert _views(session_factory, bob) == [("USD", 0, 0)]


def test_parallel_create_wallet_calls_create_exactly_one_account_and_exactly_one_reports_created(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    def create() -> tuple[uuid.UUID, bool]:
        with session_factory.begin() as session:
            view, created = create_wallet(session, user_id, "EUR")
            return view.account_id, created

    # Act
    results = _run_parallel([create] * PARALLEL)

    # Assert
    created_flags = [created for _, created in results]
    assert (created_flags.count(True), created_flags.count(False)) == (1, PARALLEL - 1)
    assert len({account_id for account_id, _ in results}) == 1
    assert _views(session_factory, user_id) == [("EUR", 0, 0), ("USD", 0, 0)]
    assert (
        _count(
            session_factory,
            "SELECT count(*) AS n FROM gateway.ledger_accounts "
            "WHERE owner_id = :uid AND kind = 'wallet' AND currency = 'EUR'",
            uid=user_id,
        )
        == 1
    )


# --- ledger_wallet_balances -------------------------------------------------------------------------------


def test_ledger_wallet_balances_for_a_new_user_is_the_zero_signup_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, ALICE)

    # Act
    with session_factory() as session:
        views = ledger_wallet_balances(session, user_id)

    # Assert
    assert [(v.account_id, v.currency, v.available_minor, v.held_minor) for v in views] == [
        (wallet_id, "USD", 0, 0)
    ]


def test_ledger_wallet_balances_equals_list_wallets_after_top_ups_in_several_currencies(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    _fund(session_factory, clock, user_id, "USD", 10_000)
    _fund(session_factory, clock, user_id, "USD", 2_500)
    _fund(session_factory, clock, user_id, "EUR", 777)
    _fund(session_factory, clock, user_id, "JPY", 50_000)
    with session_factory() as session:
        listed = list_wallets(session, user_id)
        ledger = ledger_wallet_balances(session, user_id)

    # Assert
    assert [(v.currency, v.available_minor, v.held_minor) for v in ledger] == [
        ("EUR", 777, 0),
        ("JPY", 50_000, 0),
        ("USD", 12_500, 0),
    ]
    assert ledger == listed


def test_ledger_wallet_balances_equals_list_wallets_after_top_up_service_calls(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    _top_up(session_factory, clock, user_id, "k-1", 3_000)
    _top_up(session_factory, clock, user_id, "k-2", 1_000, currency="INR")
    with session_factory() as session:
        listed = list_wallets(session, user_id)
        ledger = ledger_wallet_balances(session, user_id)

    # Assert
    assert [(v.currency, v.available_minor, v.held_minor) for v in ledger] == [
        ("INR", 1_000, 0),
        ("USD", 3_000, 0),
    ]
    assert ledger == listed


def test_ledger_wallet_balances_equals_list_wallets_for_both_sides_of_a_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    outcome = transfer(session_factory, clock, alice, "t-1", BOB, 2_500, "USD")

    # Assert
    assert outcome.status_code == 201
    assert _views(session_factory, alice, ledger=True) == [("USD", 7_500, 0)]
    assert _views(session_factory, bob, ledger=True) == [("USD", 2_500, 0)]
    with session_factory() as session:
        assert ledger_wallet_balances(session, alice) == list_wallets(session, alice)
        assert ledger_wallet_balances(session, bob) == list_wallets(session, bob)


def test_ledger_wallet_balances_reports_a_payout_hold_as_held_and_equals_list_wallets(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, user_id, "USD", 10_000)
    deps = PayoutDeps(session_factory, clock, _FakeRail(RailResult(status="unknown")))

    # Act
    outcome = create_payout(
        deps,
        user_id=user_id,
        idempotency_key="p-1",
        method="bank_transfer",
        amount_minor=2_500,
        currency="USD",
        destination=DESTINATION,
    )

    # Assert
    assert (outcome.status_code, outcome.body["state"]) == (202, "unknown")
    assert _views(session_factory, user_id, ledger=True) == [("USD", 7_500, 2_500)]
    with session_factory() as session:
        assert ledger_wallet_balances(session, user_id) == list_wallets(session, user_id)


def test_ledger_wallet_balances_is_computed_from_postings_not_from_the_balance_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, user_id, "USD", 10_000)
    with session_factory.begin() as session:
        session.execute(
            update(AccountBalance)
            .where(AccountBalance.account_id == wallet_id)
            .values(available_minor=9_999, held_minor=5)
        )

    # Act
    ledger = _views(session_factory, user_id, ledger=True)
    listed = _views(session_factory, user_id)

    # Assert
    assert listed == [("USD", 9_999, 5)]
    assert ledger == [("USD", 10_000, 0)]


def test_ledger_wallet_balances_orders_by_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE, currency="USD")
    _fund(session_factory, clock, user_id, "JPY", 1)
    _fund(session_factory, clock, user_id, "EUR", 2)
    _fund(session_factory, clock, user_id, "INR", 3)

    # Act
    ledger = _views(session_factory, user_id, ledger=True)

    # Assert
    assert ledger == [("EUR", 2, 0), ("INR", 3, 0), ("JPY", 1, 0), ("USD", 0, 0)]


def test_ledger_wallet_balances_skips_a_wallet_account_without_a_balance_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)
    with session_factory.begin() as session:
        ensure_account(session, "user", user_id, "wallet", "EUR")

    # Act
    ledger = _views(session_factory, user_id, ledger=True)

    # Assert
    assert ledger == [("USD", 0, 0)]


def test_ledger_wallet_balances_ignores_other_users(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, bob, "USD", 9_000)

    # Act
    ledger = _views(session_factory, alice, ledger=True)

    # Assert
    assert ledger == [("USD", 0, 0)]


# --- top_up -----------------------------------------------------------------------------------------------


def test_top_up_returns_201_with_the_top_up_body_and_credits_the_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", 5_000)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == {
        "top_up_id": str(_top_up_id(outcome.body)),
        "currency": "USD",
        "amount_minor": 5_000,
        "available_minor": 5_000,
    }
    assert _views(session_factory, user_id) == [("USD", 5_000, 0)]
    assert _views(session_factory, user_id, ledger=True) == [("USD", 5_000, 0)]


def test_top_up_adds_to_the_current_balance_and_reports_it_as_available_minor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)
    _top_up(session_factory, clock, user_id, "k-1", 5_000)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-2", 2_000)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["available_minor"] == 7_000
    assert _views(session_factory, user_id) == [("USD", 7_000, 0)]


def test_top_up_creates_the_wallet_for_a_currency_the_user_has_none_in(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", 1_234, currency="EUR")

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["currency"] == "EUR"
    assert outcome.body["available_minor"] == 1_234
    assert _views(session_factory, user_id) == [("EUR", 1_234, 0), ("USD", 0, 0)]


def test_top_up_posts_a_two_line_top_up_journal_with_the_top_up_id_as_ref_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, ALICE)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", 5_000)

    # Assert
    top_up_id = _top_up_id(outcome.body)
    journals = _rows(
        session_factory, "SELECT id, ref_type, ref_id FROM gateway.journal_entries ORDER BY created_at"
    )
    assert [(j["ref_type"], j["ref_id"]) for j in journals] == [("top_up", top_up_id)]
    lines = _rows(
        session_factory,
        "SELECT la.owner_type, la.owner_id, la.kind, p.account_id, p.currency, p.amount_minor "
        "FROM gateway.postings p JOIN gateway.ledger_accounts la ON la.id = p.account_id "
        "ORDER BY p.amount_minor DESC",
    )
    assert lines == [
        {
            "owner_type": "platform",
            "owner_id": None,
            "kind": "topup_clearing",
            "account_id": lines[0]["account_id"],
            "currency": "USD",
            "amount_minor": 5_000,
        },
        {
            "owner_type": "user",
            "owner_id": user_id,
            "kind": "wallet",
            "account_id": wallet_id,
            "currency": "USD",
            "amount_minor": -5_000,
        },
    ]


def test_top_up_stores_one_row_holding_the_request_and_the_returned_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, wallet_id = _user(session_factory, clock, ALICE)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", 5_000)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT id, user_id, account_id, amount_minor, currency, idempotency_key, response_body "
        "FROM gateway.wallet_top_ups",
    )
    assert rows == [
        {
            "id": _top_up_id(outcome.body),
            "user_id": user_id,
            "account_id": wallet_id,
            "amount_minor": 5_000,
            "currency": "USD",
            "idempotency_key": "k-1",
            "response_body": outcome.body,
        }
    ]


def test_top_up_replay_returns_the_stored_201_body_without_a_second_credit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)
    first = _top_up(session_factory, clock, user_id, "k-1", 5_000)
    _top_up(session_factory, clock, user_id, "k-2", 1_000)

    # Act
    replay = _top_up(session_factory, clock, user_id, "k-1", 5_000)

    # Assert
    assert (replay.status_code, replay.body) == (201, first.body)
    assert replay.body["available_minor"] == 5_000
    assert _views(session_factory, user_id) == [("USD", 6_000, 0)]
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.wallet_top_ups") == 2
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries") == 2


@pytest.mark.parametrize(
    ("amount_minor", "currency"),
    [(5_001, "USD"), (5_000, "EUR")],
    ids=["different_amount", "different_currency"],
)
def test_top_up_with_the_same_key_and_a_different_request_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock, amount_minor: int, currency: str
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)
    _top_up(session_factory, clock, user_id, "k-1", 5_000)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", amount_minor, currency=currency)

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})
    assert _views(session_factory, user_id) == [("USD", 5_000, 0)]
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.wallet_top_ups") == 1
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries") == 1


def test_top_up_idempotency_keys_are_scoped_per_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _user(session_factory, clock, ALICE)
    bob, _ = _user(session_factory, clock, BOB)
    _top_up(session_factory, clock, alice, "shared-key", 5_000)

    # Act
    outcome = _top_up(session_factory, clock, bob, "shared-key", 700)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body["amount_minor"] == 700
    assert _views(session_factory, alice) == [("USD", 5_000, 0)]
    assert _views(session_factory, bob) == [("USD", 700, 0)]


def test_top_up_writes_one_wallet_top_up_audit_row_for_the_user_and_none_on_replay(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", 5_000)
    _top_up(session_factory, clock, user_id, "k-1", 5_000)
    _top_up(session_factory, clock, user_id, "k-1", 5_001)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target FROM gateway.audit_log WHERE action LIKE 'wallet.%'",
    )
    assert len(rows) == 1
    assert (rows[0]["actor_id"], rows[0]["actor_role"], rows[0]["action"]) == (
        f"user:{user_id}",
        "consumer",
        "wallet.top_up",
    )
    assert str(_top_up_id(outcome.body)) in str(rows[0]["target"])


def test_parallel_top_ups_with_one_key_credit_the_wallet_once_and_all_answer_the_same_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    outcomes = _run_parallel(
        [lambda: _top_up(session_factory, clock, user_id, "same-key", 5_000) for _ in range(PARALLEL)]
    )

    # Assert
    assert {outcome.status_code for outcome in outcomes} == {201}
    assert len({_top_up_id(outcome.body) for outcome in outcomes}) == 1
    assert _views(session_factory, user_id) == [("USD", 5_000, 0)]
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.wallet_top_ups") == 1
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries") == 1


def test_parallel_top_ups_with_distinct_keys_credit_every_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    outcomes = _run_parallel(
        [functools.partial(_top_up, session_factory, clock, user_id, f"k-{i}", 100) for i in range(PARALLEL)]
    )

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * PARALLEL
    assert sorted(int(str(outcome.body["available_minor"])) for outcome in outcomes) == [
        100 * n for n in range(1, PARALLEL + 1)
    ]
    assert _views(session_factory, user_id) == [("USD", 100 * PARALLEL, 0)]
    assert _views(session_factory, user_id, ledger=True) == [("USD", 100 * PARALLEL, 0)]


def test_a_jpy_top_up_keeps_the_amount_in_minor_units_of_exponent_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _user(session_factory, clock, ALICE)

    # Act
    outcome = _top_up(session_factory, clock, user_id, "k-1", 15_150, currency="JPY")

    # Assert
    assert outcome.body["amount_minor"] == 15_150
    assert _views(session_factory, user_id) == [("JPY", 15_150, 0), ("USD", 0, 0)]
    assert outcome.body["available_minor"] == 15_150
