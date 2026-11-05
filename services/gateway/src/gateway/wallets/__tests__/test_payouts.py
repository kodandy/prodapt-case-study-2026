"""Tests for `create_payout`, `settle_payout` and `get_payout`, written from docs/specs/gateway-basic.md
section B1.4 / B2.3 / B2.5 / P0-2 / P0-3 (Decisions 8 and 9, `gateway/wallets/payouts.py`).

`RailResult` is imported from the `gateway.payments.rails` package, so its `__init__` must re-export it.
Journal `ref_id` of the three payout journals is assumed to be the payout id.
"""

import threading
import uuid
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor

import pytest
from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from gateway.payments.rails import RailResult
from gateway.wallets.payouts import (
    PayoutDeps,
    PayoutNotFoundError,
    PayoutOutcome,
    create_payout,
    get_payout,
    settle_payout,
)
from gateway.wallets.service import ledger_wallet_balances, list_wallets, top_up_wallet, transfer
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"
ALICE = "alice@example.com"
BOB = "bob@example.com"
PARALLEL = 8
DESTINATION = "DE89370400440532013000"
MASKED = "****3000"
AMOUNT = 2_500

SUCCEEDED = RailResult(status="succeeded")
FAILED = RailResult(status="failed", failure_code="bank_declined")
UNKNOWN = RailResult(status="unknown")


class _ScriptedRail:
    """Answers from a script, then from `default`; an exception in the script is raised. Thread-safe."""

    name = "scripted"

    def __init__(
        self,
        *script: RailResult | Exception,
        default: RailResult = SUCCEEDED,
        on_submit: Callable[[str], None] | None = None,
    ) -> None:
        self._script = list(script)
        self._default = default
        self._on_submit = on_submit
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str, int, str, str]] = []

    def submit(
        self, payout_id: str, method: str, amount_minor: int, currency: str, destination: str
    ) -> RailResult:
        with self._lock:
            self.calls.append((payout_id, method, amount_minor, currency, destination))
            answer = self._script.pop(0) if self._script else self._default
        if self._on_submit is not None:
            self._on_submit(payout_id)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _user(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str, currency: str = "USD"
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD, currency=currency)
        session.commit()
    return result.user_id


def _fund(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    amount_minor: int,
    currency: str = "USD",
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, currency, amount_minor)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str, **params: object) -> int:
    return int(str(_rows(session_factory, query, **params)[0]["n"]))


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _ledger(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        views = ledger_wallet_balances(session, user_id)
        return [(v.currency, v.available_minor, v.held_minor) for v in views]


def _journal_count(session_factory: sessionmaker[Session], ref_type: str) -> int:
    return _count(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = :ref_type",
        ref_type=ref_type,
    )


def _journal_lines(
    session_factory: sessionmaker[Session], ref_type: str
) -> list[tuple[uuid.UUID, str, uuid.UUID | None, str, str, int]]:
    rows = _rows(
        session_factory,
        "SELECT j.ref_id, la.owner_type, la.owner_id, la.kind, p.currency, p.amount_minor "
        "FROM gateway.journal_entries j "
        "JOIN gateway.postings p ON p.journal_id = j.id "
        "JOIN gateway.ledger_accounts la ON la.id = p.account_id "
        "WHERE j.ref_type = :ref_type ORDER BY p.amount_minor DESC",
        ref_type=ref_type,
    )
    return [
        (
            uuid.UUID(str(r["ref_id"])),
            str(r["owner_type"]),
            None if r["owner_id"] is None else uuid.UUID(str(r["owner_id"])),
            str(r["kind"]),
            str(r["currency"]),
            int(str(r["amount_minor"])),
        )
        for r in rows
    ]


def _payout_counts(session_factory: sessionmaker[Session]) -> tuple[int, int, int]:
    """Journal counts: (payout_hold, payout_capture, payout_release)."""
    return (
        _journal_count(session_factory, "payout_hold"),
        _journal_count(session_factory, "payout_capture"),
        _journal_count(session_factory, "payout_release"),
    )


def _posted(session_factory: sessionmaker[Session], user_id: uuid.UUID, kind: str, currency: str) -> int:
    """Minus the sum of postings on the user's account of `kind` in `currency`."""
    rows = _rows(
        session_factory,
        "SELECT (-COALESCE(SUM(p.amount_minor), 0))::bigint AS n FROM gateway.postings p "
        "JOIN gateway.ledger_accounts la ON la.id = p.account_id "
        "WHERE la.owner_type = 'user' AND la.owner_id = :uid AND la.kind = :kind AND la.currency = :ccy",
        uid=user_id,
        kind=kind,
        ccy=currency,
    )
    return int(str(rows[0]["n"]))


def _payout_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, user_id, method, amount_minor, currency, destination_masked, state, failure_code, "
        "idempotency_key FROM gateway.payouts ORDER BY created_at, id",
    )


def _payout_audit_count(session_factory: sessionmaker[Session]) -> int:
    return _count(
        session_factory, "SELECT count(*) AS n FROM gateway.audit_log WHERE action LIKE 'payout.%'"
    )


def _deps(session_factory: sessionmaker[Session], clock: FixedClock, rail: _ScriptedRail) -> PayoutDeps:
    return PayoutDeps(session_factory=session_factory, clock=clock, rail=rail)


def _create(
    deps: PayoutDeps,
    user_id: uuid.UUID,
    key: str = "p-1",
    amount_minor: int = AMOUNT,
    destination: str = DESTINATION,
    method: str = "bank_transfer",
    currency: str = "USD",
) -> PayoutOutcome:
    return create_payout(
        deps,
        user_id=user_id,
        idempotency_key=key,
        method=method,
        amount_minor=amount_minor,
        currency=currency,
        destination=destination,
    )


def _pid(outcome: PayoutOutcome) -> uuid.UUID:
    return uuid.UUID(str(outcome.body["payout_id"]))


def _view_body(
    payout_id: uuid.UUID,
    state: str,
    failure_code: str | None = None,
    method: str = "bank_transfer",
    amount_minor: int = AMOUNT,
    masked: str = MASKED,
) -> dict[str, object]:
    return {
        "payout_id": str(payout_id),
        "state": state,
        "method": method,
        "amount_minor": amount_minor,
        "currency": "USD",
        "destination_masked": masked,
        "failure_code": failure_code,
    }


def _run_parallel[T](jobs: Sequence[Callable[[], T]]) -> list[T]:
    barrier = threading.Barrier(len(jobs))

    def run(job: Callable[[], T]) -> T:
        barrier.wait(timeout=30)
        return job()

    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [pool.submit(run, job) for job in jobs]
        return [future.result(timeout=30) for future in futures]


def _setup(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    rail: _ScriptedRail,
    balance: int = 10_000,
) -> tuple[PayoutDeps, uuid.UUID]:
    user_id = _user(session_factory, clock, ALICE)
    _fund(session_factory, clock, user_id, balance)
    return _deps(session_factory, clock, rail), user_id


# --- success: hold, rail, capture -------------------------------------------------------------------------


def test_create_payout_that_the_rail_accepts_returns_201_succeeded_and_captures_the_hold(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED)
    deps, user_id = _setup(session_factory, clock, rail)

    # Act
    outcome = _create(deps, user_id)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == _view_body(_pid(outcome), "succeeded")
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 0)]
    assert _ledger(session_factory, user_id) == [("USD", 7_500, 0)]
    assert _payout_counts(session_factory) == (1, 1, 0)


def test_create_payout_books_the_hold_journal_wallet_debit_and_wallet_hold_credit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    pid = _pid(outcome)
    assert _journal_lines(session_factory, "payout_hold") == [
        (pid, "user", user_id, "wallet", "USD", AMOUNT),
        (pid, "user", user_id, "wallet_hold", "USD", -AMOUNT),
    ]


def test_create_payout_books_the_capture_journal_from_wallet_hold_into_payout_clearing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    pid = _pid(outcome)
    assert _journal_lines(session_factory, "payout_capture") == [
        (pid, "user", user_id, "wallet_hold", "USD", AMOUNT),
        (pid, "platform", None, "payout_clearing", "USD", -AMOUNT),
    ]
    assert _posted(session_factory, user_id, "wallet_hold", "USD") == 0
    assert _posted(session_factory, user_id, "wallet", "USD") == 7_500


def test_create_payout_stores_one_succeeded_row_with_the_request_fields(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED))

    # Act
    outcome = _create(deps, user_id, key="p-key")

    # Assert
    assert _payout_rows(session_factory) == [
        {
            "id": _pid(outcome),
            "user_id": user_id,
            "method": "bank_transfer",
            "amount_minor": AMOUNT,
            "currency": "USD",
            "destination_masked": MASKED,
            "state": "succeeded",
            "failure_code": None,
            "idempotency_key": "p-key",
        }
    ]


def test_create_payout_calls_the_rail_once_with_the_payout_id_as_its_key_and_the_raw_destination(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED)
    deps, user_id = _setup(session_factory, clock, rail)

    # Act
    outcome = _create(deps, user_id, method="upi", destination="alice.pay@okbank")

    # Assert
    assert rail.calls == [(str(_pid(outcome)), "upi", AMOUNT, "USD", "alice.pay@okbank")]
    assert outcome.body["destination_masked"] == "****bank"
    assert outcome.body["method"] == "upi"


def test_create_payout_writes_one_payout_created_audit_row_for_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary::text AS summary_text "
        "FROM gateway.audit_log WHERE action = 'payout.created'",
    )
    assert len(rows) == 1
    assert (rows[0]["actor_id"], rows[0]["actor_role"]) == (f"user:{user_id}", "consumer")
    assert str(_pid(outcome)) in str(rows[0]["target"])
    assert DESTINATION not in str(rows[0]["summary_text"])


# --- tx1 commits before the rail is called ----------------------------------------------------------------


def test_the_hold_is_committed_and_no_lock_is_held_while_the_rail_runs(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    observed: list[dict[str, object]] = []
    wallet_ids: list[uuid.UUID] = []

    def observe(payout_id: str) -> None:
        with session_factory.begin() as session:
            try:
                session.execute(
                    text(
                        "SELECT 1 FROM gateway.account_balances WHERE account_id = :a FOR UPDATE NOWAIT"
                    ),
                    {"a": wallet_ids[0]},
                )
                session.execute(
                    text("SELECT 1 FROM gateway.payouts WHERE id = :p FOR UPDATE NOWAIT"),
                    {"p": uuid.UUID(payout_id)},
                )
                locks = "free"
            except OperationalError:
                locks = "held"
        row = _payout_rows(session_factory)[0]
        wallets = _wallets(session_factory, user_id)
        observed.append({"locks": locks, "state": row["state"], "wallets": wallets})

    rail = _ScriptedRail(SUCCEEDED, on_submit=observe)
    deps, user_id = _setup(session_factory, clock, rail)
    with session_factory() as session:
        wallet_ids.append(list_wallets(session, user_id)[0].account_id)

    # Act
    _create(deps, user_id)

    # Assert
    assert observed == [{"locks": "free", "state": "pending", "wallets": [("USD", 7_500, 2_500)]}]
    assert _payout_rows(session_factory)[0]["state"] == "succeeded"


# --- failure: release -------------------------------------------------------------------------------------


def test_create_payout_that_the_rail_rejects_returns_201_failed_and_releases_the_hold(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(FAILED))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == _view_body(_pid(outcome), "failed", failure_code="bank_declined")
    assert _wallets(session_factory, user_id) == [("USD", 10_000, 0)]
    assert _ledger(session_factory, user_id) == [("USD", 10_000, 0)]
    assert _payout_counts(session_factory) == (1, 0, 1)


def test_a_failed_payout_books_the_release_journal_and_stores_the_failure_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(FAILED))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    pid = _pid(outcome)
    assert _journal_lines(session_factory, "payout_release") == [
        (pid, "user", user_id, "wallet_hold", "USD", AMOUNT),
        (pid, "user", user_id, "wallet", "USD", -AMOUNT),
    ]
    assert [(r["state"], r["failure_code"]) for r in _payout_rows(session_factory)] == [
        ("failed", "bank_declined")
    ]
    assert _posted(session_factory, user_id, "wallet_hold", "USD") == 0
    assert _posted(session_factory, user_id, "wallet", "USD") == 10_000


# --- unknown: funds stay held -----------------------------------------------------------------------------


def test_create_payout_with_an_unknown_rail_answer_returns_202_and_keeps_the_funds_held(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    assert outcome.status_code == 202
    assert outcome.body == _view_body(_pid(outcome), "unknown")
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 2_500)]
    assert _ledger(session_factory, user_id) == [("USD", 7_500, 2_500)]
    assert _payout_counts(session_factory) == (1, 0, 0)
    assert _posted(session_factory, user_id, "wallet_hold", "USD") == 2_500


@pytest.mark.parametrize("error", [TimeoutError("rail timed out"), ConnectionError("rail unreachable")])
def test_create_payout_treats_a_rail_timeout_or_connection_error_as_unknown(
    session_factory: sessionmaker[Session], clock: FixedClock, error: Exception
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(error))

    # Act
    outcome = _create(deps, user_id)

    # Assert
    assert outcome.status_code == 202
    assert outcome.body == _view_body(_pid(outcome), "unknown")
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 2_500)]
    assert _payout_counts(session_factory) == (1, 0, 0)


# --- settle_payout ----------------------------------------------------------------------------------------


def test_settle_payout_of_an_unknown_payout_with_success_captures_the_held_funds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))

    # Act
    view = settle_payout(session_factory, clock, pid, SUCCEEDED)

    # Assert
    assert view.model_dump(mode="json") == _view_body(pid, "succeeded")
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 0)]
    assert _ledger(session_factory, user_id) == [("USD", 7_500, 0)]
    assert _payout_counts(session_factory) == (1, 1, 0)
    assert _journal_lines(session_factory, "payout_capture") == [
        (pid, "user", user_id, "wallet_hold", "USD", AMOUNT),
        (pid, "platform", None, "payout_clearing", "USD", -AMOUNT),
    ]


def test_settle_payout_of_an_unknown_payout_with_failure_releases_the_funds_and_stores_the_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))

    # Act
    view = settle_payout(session_factory, clock, pid, FAILED)

    # Assert
    assert view.model_dump(mode="json") == _view_body(pid, "failed", failure_code="bank_declined")
    assert _wallets(session_factory, user_id) == [("USD", 10_000, 0)]
    assert _ledger(session_factory, user_id) == [("USD", 10_000, 0)]
    assert _payout_counts(session_factory) == (1, 0, 1)
    assert [(r["state"], r["failure_code"]) for r in _payout_rows(session_factory)] == [
        ("failed", "bank_declined")
    ]


def test_settle_payout_with_unknown_keeps_an_unknown_payout_held_and_posts_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))

    # Act
    view = settle_payout(session_factory, clock, pid, UNKNOWN)

    # Assert
    assert view.model_dump(mode="json") == _view_body(pid, "unknown")
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 2_500)]
    assert _payout_counts(session_factory) == (1, 0, 0)


@pytest.mark.parametrize(
    ("first", "state", "expected_wallet"),
    [(SUCCEEDED, "succeeded", ("USD", 7_500, 0)), (FAILED, "failed", ("USD", 10_000, 0))],
    ids=["capture", "release"],
)
def test_a_duplicate_settle_is_a_no_op(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    first: RailResult,
    state: str,
    expected_wallet: tuple[str, int, int],
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))
    settled = settle_payout(session_factory, clock, pid, first)
    counts = _payout_counts(session_factory)
    rows = _payout_rows(session_factory)

    # Act
    again = settle_payout(session_factory, clock, pid, first)

    # Assert
    assert again == settled
    assert again.state == state
    assert _payout_counts(session_factory) == counts
    assert _payout_rows(session_factory) == rows
    assert _wallets(session_factory, user_id) == [expected_wallet]
    assert _ledger(session_factory, user_id) == [expected_wallet]


@pytest.mark.parametrize(
    ("first", "second", "state", "expected_wallet", "expected_counts"),
    [
        (SUCCEEDED, FAILED, "succeeded", ("USD", 7_500, 0), (1, 1, 0)),
        (FAILED, SUCCEEDED, "failed", ("USD", 10_000, 0), (1, 0, 1)),
        (SUCCEEDED, UNKNOWN, "succeeded", ("USD", 7_500, 0), (1, 1, 0)),
        (FAILED, UNKNOWN, "failed", ("USD", 10_000, 0), (1, 0, 1)),
    ],
    ids=["capture_then_release", "release_then_capture", "capture_then_unknown", "release_then_unknown"],
)
def test_a_contradictory_settle_after_a_final_state_is_a_no_op(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    first: RailResult,
    second: RailResult,
    state: str,
    expected_wallet: tuple[str, int, int],
    expected_counts: tuple[int, int, int],
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))
    settled = settle_payout(session_factory, clock, pid, first)

    # Act
    again = settle_payout(session_factory, clock, pid, second)

    # Assert
    assert again == settled
    assert again.state == state
    assert _payout_counts(session_factory) == expected_counts
    assert _wallets(session_factory, user_id) == [expected_wallet]
    assert _ledger(session_factory, user_id) == [expected_wallet]


def test_settle_payout_for_an_unknown_payout_id_raises_payout_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _setup(session_factory, clock, _ScriptedRail())

    # Act / Assert
    with pytest.raises(PayoutNotFoundError):
        settle_payout(session_factory, clock, uuid.uuid4(), SUCCEEDED)
    assert _payout_counts(session_factory) == (0, 0, 0)


def test_the_capture_moves_exactly_the_held_amount_when_other_funds_are_held(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(UNKNOWN, UNKNOWN)
    deps, user_id = _setup(session_factory, clock, rail)
    first = _pid(_create(deps, user_id, key="p-1", amount_minor=1_000, destination="acct-0001-aaaa"))
    _create(deps, user_id, key="p-2", amount_minor=3_000, destination="acct-0002-bbbb")

    # Act
    settle_payout(session_factory, clock, first, SUCCEEDED)

    # Assert
    assert _wallets(session_factory, user_id) == [("USD", 6_000, 3_000)]
    assert _ledger(session_factory, user_id) == [("USD", 6_000, 3_000)]
    assert _journal_lines(session_factory, "payout_capture") == [
        (first, "user", user_id, "wallet_hold", "USD", 1_000),
        (first, "platform", None, "payout_clearing", "USD", -1_000),
    ]


# --- insufficient funds -----------------------------------------------------------------------------------


def test_create_payout_above_the_available_balance_returns_409_and_stores_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED)
    deps, user_id = _setup(session_factory, clock, rail, balance=1_000)

    # Act
    outcome = _create(deps, user_id, amount_minor=1_001)

    # Assert
    assert outcome.status_code == 409
    assert outcome.body == {"error": "insufficient_funds", "available_minor": 1_000}
    assert _payout_rows(session_factory) == []
    assert _payout_counts(session_factory) == (0, 0, 0)
    assert rail.calls == []
    assert _wallets(session_factory, user_id) == [("USD", 1_000, 0)]
    assert _payout_audit_count(session_factory) == 0


def test_create_payout_of_exactly_the_available_balance_succeeds_and_leaves_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED), balance=1_000)

    # Act
    outcome = _create(deps, user_id, amount_minor=1_000)

    # Assert
    assert outcome.status_code == 201
    assert _wallets(session_factory, user_id) == [("USD", 0, 0)]


def test_create_payout_counts_only_available_funds_not_held_funds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN), balance=3_000)
    _create(deps, user_id, key="p-1", amount_minor=2_000)

    # Act
    outcome = _create(deps, user_id, key="p-2", amount_minor=1_500, destination="acct-0002-bbbb")

    # Assert
    assert outcome.status_code == 409
    assert outcome.body == {"error": "insufficient_funds", "available_minor": 1_000}
    assert _wallets(session_factory, user_id) == [("USD", 1_000, 2_000)]


def test_an_insufficient_funds_answer_is_not_stored_so_the_key_works_after_a_top_up(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED), balance=1_000)
    rejected = _create(deps, user_id, key="p-1", amount_minor=2_000)
    _fund(session_factory, clock, user_id, 5_000)

    # Act
    retry = _create(deps, user_id, key="p-1", amount_minor=2_000)

    # Assert
    assert rejected.status_code == 409
    assert retry.status_code == 201
    assert _wallets(session_factory, user_id) == [("USD", 4_000, 0)]


# --- idempotency ------------------------------------------------------------------------------------------


def test_a_same_key_same_request_replay_returns_the_same_answer_without_a_second_hold_or_rail_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED)
    deps, user_id = _setup(session_factory, clock, rail)
    first = _create(deps, user_id)

    # Act
    replay = _create(deps, user_id)

    # Assert
    assert (replay.status_code, replay.body) == (first.status_code, first.body)
    assert len(rail.calls) == 1
    assert len(_payout_rows(session_factory)) == 1
    assert _payout_counts(session_factory) == (1, 1, 0)
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 0)]
    assert _payout_audit_count(session_factory) == 1


def test_a_replay_of_an_unknown_payout_answers_202_unknown_without_calling_the_rail_again(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(UNKNOWN)
    deps, user_id = _setup(session_factory, clock, rail)
    first = _create(deps, user_id)

    # Act
    replay = _create(deps, user_id)

    # Assert
    assert (replay.status_code, replay.body) == (202, first.body)
    assert len(rail.calls) == 1
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 2_500)]


def test_a_replay_after_a_later_settle_returns_the_current_view(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(UNKNOWN)
    deps, user_id = _setup(session_factory, clock, rail)
    pid = _pid(_create(deps, user_id))
    settle_payout(session_factory, clock, pid, FAILED)

    # Act
    replay = _create(deps, user_id)

    # Assert
    assert replay.status_code == 201
    assert replay.body == _view_body(pid, "failed", failure_code="bank_declined")
    assert len(rail.calls) == 1
    assert _wallets(session_factory, user_id) == [("USD", 10_000, 0)]


@pytest.mark.parametrize(
    "changed",
    [
        {"amount_minor": AMOUNT + 1},
        {"destination": "DE89370400440532019999"},
        {"destination": "XX00000000000003000"},
        {"method": "upi"},
    ],
    ids=["amount", "destination_other_last_4", "destination_same_last_4", "method"],
)
def test_the_same_key_with_a_different_request_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock, changed: dict[str, object]
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED)
    deps, user_id = _setup(session_factory, clock, rail)
    _create(deps, user_id)
    request: dict[str, object] = {
        "amount_minor": AMOUNT,
        "destination": DESTINATION,
        "method": "bank_transfer",
        **changed,
    }

    # Act
    outcome = create_payout(
        deps,
        user_id=user_id,
        idempotency_key="p-1",
        method=str(request["method"]),
        amount_minor=int(str(request["amount_minor"])),
        currency="USD",
        destination=str(request["destination"]),
    )

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})
    assert len(rail.calls) == 1
    assert len(_payout_rows(session_factory)) == 1
    assert _wallets(session_factory, user_id) == [("USD", 7_500, 0)]


def test_the_same_key_with_a_different_currency_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED)
    deps, user_id = _setup(session_factory, clock, rail)
    _fund(session_factory, clock, user_id, 10_000, currency="EUR")
    _create(deps, user_id)

    # Act
    outcome = _create(deps, user_id, currency="EUR")

    # Assert
    assert (outcome.status_code, outcome.body) == (422, {"error": "idempotency_key_reused"})
    assert _wallets(session_factory, user_id) == [("EUR", 10_000, 0), ("USD", 7_500, 0)]


def test_idempotency_keys_are_scoped_per_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED, SUCCEEDED)
    deps, alice = _setup(session_factory, clock, rail)
    bob = _user(session_factory, clock, BOB)
    _fund(session_factory, clock, bob, 4_000)
    first = _create(deps, alice, key="shared")

    # Act
    second = _create(deps, bob, key="shared")

    # Assert
    assert second.status_code == 201
    assert _pid(second) != _pid(first)
    assert len(rail.calls) == 2
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _wallets(session_factory, bob) == [("USD", 1_500, 0)]


# --- the raw destination is never stored ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("destination", "masked"),
    [
        ("DE89370400440532013000", "****3000"),
        ("alice.pay@okbank", "****bank"),
        ("12345-secret-destination-0099", "****0099"),
    ],
)
def test_the_raw_destination_is_stored_nowhere_and_the_mask_is_four_stars_plus_the_last_four(
    session_factory: sessionmaker[Session], clock: FixedClock, destination: str, masked: str
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))

    # Act
    outcome = _create(deps, user_id, destination=destination)

    # Assert
    payout_text = _rows(session_factory, "SELECT p::text AS t FROM gateway.payouts p")
    audit_text = _rows(session_factory, "SELECT a::text AS t FROM gateway.audit_log a")
    assert [r["destination_masked"] for r in _payout_rows(session_factory)] == [masked]
    assert outcome.body["destination_masked"] == masked
    assert destination not in str(outcome.body.values())
    assert [destination in str(r["t"]) for r in payout_text] == [False]
    assert [destination in str(r["t"]) for r in audit_text] == [False] * len(audit_text)


def test_a_four_character_destination_is_masked_to_four_stars_plus_all_four_characters(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED))

    # Act
    outcome = _create(deps, user_id, destination="ab12")

    # Assert
    assert outcome.body["destination_masked"] == "****ab12"
    assert [r["destination_masked"] for r in _payout_rows(session_factory)] == ["****ab12"]


def test_the_raw_destination_is_not_in_the_settled_view(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))

    # Act
    view = settle_payout(session_factory, clock, pid, SUCCEEDED)

    # Assert
    assert view.destination_masked == MASKED
    assert DESTINATION not in view.model_dump_json()


# --- get_payout -------------------------------------------------------------------------------------------


def test_get_payout_returns_the_owners_current_view(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, user_id = _setup(session_factory, clock, _ScriptedRail(UNKNOWN))
    pid = _pid(_create(deps, user_id))

    # Act
    with session_factory() as session:
        before = get_payout(session, user_id, pid)
    settle_payout(session_factory, clock, pid, FAILED)
    with session_factory() as session:
        after = get_payout(session, user_id, pid)

    # Assert
    assert before.model_dump(mode="json") == _view_body(pid, "unknown")
    assert after.model_dump(mode="json") == _view_body(pid, "failed", failure_code="bank_declined")


def test_get_payout_of_another_users_payout_raises_payout_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, alice = _setup(session_factory, clock, _ScriptedRail(SUCCEEDED))
    bob = _user(session_factory, clock, BOB)
    pid = _pid(_create(deps, alice))

    # Act / Assert
    with session_factory() as session, pytest.raises(PayoutNotFoundError):
        get_payout(session, bob, pid)


def test_get_payout_of_an_unknown_id_raises_payout_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _, user_id = _setup(session_factory, clock, _ScriptedRail())

    # Act / Assert
    with session_factory() as session, pytest.raises(PayoutNotFoundError):
        get_payout(session, user_id, uuid.uuid4())


# --- ledger equality across a whole sequence --------------------------------------------------------------


def test_list_wallets_equals_ledger_wallet_balances_after_every_step_of_a_payout_sequence(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED, FAILED, UNKNOWN, TimeoutError("late"))
    deps, user_id = _setup(session_factory, clock, rail, balance=10_000)
    steps: list[tuple[list[tuple[str, int, int]], list[tuple[str, int, int]]]] = []

    def snapshot() -> None:
        steps.append((_wallets(session_factory, user_id), _ledger(session_factory, user_id)))

    # Act
    snapshot()
    _create(deps, user_id, key="p-1", amount_minor=1_000, destination="acct-0001-aaaa")
    snapshot()
    _create(deps, user_id, key="p-2", amount_minor=2_000, destination="acct-0002-bbbb")
    snapshot()
    unknown = _pid(_create(deps, user_id, key="p-3", amount_minor=3_000, destination="acct-0003-cccc"))
    snapshot()
    other_unknown = _pid(_create(deps, user_id, key="p-4", amount_minor=500, destination="acct-0004-dddd"))
    snapshot()
    settle_payout(session_factory, clock, unknown, SUCCEEDED)
    snapshot()
    settle_payout(session_factory, clock, other_unknown, FAILED)
    snapshot()

    # Assert
    assert [wallets for wallets, _ in steps] == [ledger for _, ledger in steps]
    assert [wallets for wallets, _ in steps] == [
        [("USD", 10_000, 0)],
        [("USD", 9_000, 0)],
        [("USD", 9_000, 0)],
        [("USD", 6_000, 3_000)],
        [("USD", 5_500, 3_500)],
        [("USD", 5_500, 500)],
        [("USD", 6_000, 0)],
    ]


def test_every_payout_journal_sums_to_zero_per_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail(SUCCEEDED, FAILED, UNKNOWN)
    deps, user_id = _setup(session_factory, clock, rail)
    _create(deps, user_id, key="p-1", amount_minor=1_000, destination="acct-0001-aaaa")
    _create(deps, user_id, key="p-2", amount_minor=2_000, destination="acct-0002-bbbb")
    unknown = _pid(_create(deps, user_id, key="p-3", amount_minor=3_000, destination="acct-0003-cccc"))
    settle_payout(session_factory, clock, unknown, SUCCEEDED)

    # Act
    unbalanced = _rows(
        session_factory,
        "SELECT j.id FROM gateway.journal_entries j JOIN gateway.postings p ON p.journal_id = j.id "
        "GROUP BY j.id, p.currency HAVING sum(p.amount_minor) <> 0",
    )

    # Assert
    assert unbalanced == []
    assert _payout_counts(session_factory) == (3, 2, 1)


# --- P0-3: parallel debits never overdraw -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected_wallet"),
    [(SUCCEEDED, ("USD", 500, 0)), (UNKNOWN, ("USD", 500, 3_000))],
    ids=["rail_succeeds", "rail_unknown"],
)
def test_parallel_payouts_from_one_wallet_never_overdraw(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    answer: RailResult,
    expected_wallet: tuple[str, int, int],
) -> None:
    # Arrange
    rail = _ScriptedRail(default=answer)
    deps, user_id = _setup(session_factory, clock, rail, balance=3_500)
    amount = 1_000

    # Act
    outcomes = _run_parallel(
        [
            lambda i=i: _create(
                deps, user_id, key=f"p-{i}", amount_minor=amount, destination=f"acct-{i:04d}-xyz"
            )
            for i in range(PARALLEL)
        ]
    )

    # Assert
    statuses = sorted(outcome.status_code for outcome in outcomes)
    succeeded = 3_500 // amount
    assert statuses.count(409) == PARALLEL - succeeded
    assert statuses.count(201 if answer is SUCCEEDED else 202) == succeeded
    assert len(rail.calls) == succeeded
    assert len(_payout_rows(session_factory)) == succeeded
    assert _wallets(session_factory, user_id) == [expected_wallet]
    assert _wallets(session_factory, user_id) == _ledger(session_factory, user_id)
    assert expected_wallet[1] >= 0


def test_parallel_payouts_that_all_fit_are_all_accepted(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail()
    deps, user_id = _setup(session_factory, clock, rail, balance=PARALLEL * 100)

    # Act
    outcomes = _run_parallel(
        [
            lambda i=i: _create(
                deps, user_id, key=f"p-{i}", amount_minor=100, destination=f"acct-{i:04d}-xyz"
            )
            for i in range(PARALLEL)
        ]
    )

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * PARALLEL
    assert _wallets(session_factory, user_id) == [("USD", 0, 0)]
    assert _wallets(session_factory, user_id) == _ledger(session_factory, user_id)


def test_parallel_transfers_and_payouts_from_one_wallet_never_overdraw(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    rail = _ScriptedRail()
    deps, alice = _setup(session_factory, clock, rail, balance=3_500)
    bob = _user(session_factory, clock, BOB)
    half = PARALLEL // 2
    payout_jobs: list[Callable[[], int]] = [
        lambda i=i: _create(
            deps, alice, key=f"p-{i}", amount_minor=1_000, destination=f"acct-{i:04d}-xyz"
        ).status_code
        for i in range(half)
    ]
    transfer_jobs: list[Callable[[], int]] = [
        lambda i=i: transfer(session_factory, clock, alice, f"t-{i}", BOB, 1_000, "USD").status_code
        for i in range(half)
    ]

    # Act
    statuses = _run_parallel([*payout_jobs, *transfer_jobs])

    # Assert
    payouts_ok = statuses[:half].count(201)
    transfers_ok = statuses[half:].count(201)
    assert statuses.count(201) == 3
    assert statuses.count(409) == PARALLEL - 3
    assert _wallets(session_factory, alice) == [("USD", 500, 0)]
    assert _wallets(session_factory, bob) == [("USD", 1_000 * transfers_ok, 0)]
    assert _wallets(session_factory, alice) == _ledger(session_factory, alice)
    assert _wallets(session_factory, bob) == _ledger(session_factory, bob)
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.payouts") == payouts_ok
    assert _count(session_factory, "SELECT count(*) AS n FROM gateway.transfers") == transfers_ok
