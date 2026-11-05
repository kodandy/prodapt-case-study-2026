"""P0-2: random operation sequences keep the ledger balanced and the wallet balances equal to their postings.
Written from docs/specs/gateway-basic.md section "B1.4 / B2.3 / B2.5 / P0-2 / P0-3" ("P0-2 property test").

Operations: top-up, same- and cross-currency transfer, card charge (approve / decline, with or without
`settlement_currency`), partial refund of a succeeded charge, payout with a scripted rail answer and a later
settle of a payout. After every step:

- every journal sums to zero per currency;
- a journal carries an `fx_rate_id` exactly when it has postings in two currencies;
- for every user `list_wallets == ledger_wallet_balances`, and both equal the raw sums of the postings;
- all balances are >= 0;
- payout journals and the `payout_clearing` account match the payout states.

The DB fixture is function-scoped, so every example truncates the gateway and simulator tables itself. The
migration-seeded `gateway.fx_rates` rows are kept.
"""

import dataclasses
import uuid
from typing import Literal

from gateway.accounts.models import Merchant
from gateway.accounts.service import WALLET_KIND, sign_up
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.rails import RailResult
from gateway.payments.refunds import refund_payment
from gateway.payments.schemas import ChargeRequest
from gateway.wallets.payouts import PayoutDeps, create_payout, settle_payout
from gateway.wallets.service import (
    HOLD_KIND,
    ledger_wallet_balances,
    list_wallets,
    top_up_wallet,
    transfer,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

PASSWORD = "correct horse battery staple"
APPROVE_PAN = "4242424242424242"
DECLINE_PAN = "4000000000000002"
DESTINATION = "DE89370400440532013000"
CURRENCIES = ("USD", "EUR", "JPY", "INR")
PAYOUT_METHODS = ("bank_transfer", "upi")
RESET_SCHEMAS = ("gateway", "simulator")
KEPT_TABLES = ("alembic_version", "fx_rates")
MAX_STEP_AMOUNT = 50_000
"""Below every risk `high_amount` threshold: no charge is risk-blocked, approve / decline is exact."""

RailAnswer = Literal["succeeded", "failed", "unknown", "timeout"]
SettleAnswer = Literal["succeeded", "failed", "unknown"]
PAYOUT_STATE_BY_ANSWER: dict[str, str] = {
    "succeeded": "succeeded",
    "failed": "failed",
    "unknown": "unknown",
    "timeout": "unknown",
}
PAYOUT_STATUS_BY_STATE = {"succeeded": 201, "failed": 201, "unknown": 202}

PROPERTY_SETTINGS = settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


# --- steps ---------------------------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TopUp:
    user: int
    currency: str
    amount_minor: int


@dataclasses.dataclass(frozen=True)
class Transfer:
    sender: int
    offset: int
    """Recipient = sender + 1 + (offset - 1) % (users - 1) (mod users), so a user never sends to themself."""
    currency: str
    recipient_currency: str | None
    amount_minor: int


@dataclasses.dataclass(frozen=True)
class Charge:
    approve: bool
    currency: str
    settlement_currency: str | None
    amount_minor: int


@dataclasses.dataclass(frozen=True)
class Refund:
    pick: int
    permille: int


@dataclasses.dataclass(frozen=True)
class Payout:
    user: int
    method: str
    currency: str
    amount_minor: int
    answer: RailAnswer


@dataclasses.dataclass(frozen=True)
class Settle:
    pick: int
    answer: SettleAnswer


Step = TopUp | Transfer | Charge | Refund | Payout | Settle

_USER = st.integers(min_value=0, max_value=2)
_CURRENCY = st.sampled_from(CURRENCIES)
_AMOUNT = st.integers(min_value=1, max_value=MAX_STEP_AMOUNT)
_TOP_UP = st.builds(TopUp, user=_USER, currency=_CURRENCY, amount_minor=_AMOUNT)
STEPS = st.one_of(
    _TOP_UP,
    _TOP_UP,
    st.builds(
        Transfer,
        sender=_USER,
        offset=st.integers(min_value=1, max_value=2),
        currency=_CURRENCY,
        recipient_currency=st.one_of(st.none(), _CURRENCY),
        amount_minor=_AMOUNT,
    ),
    st.builds(
        Charge,
        approve=st.booleans(),
        currency=_CURRENCY,
        settlement_currency=st.one_of(st.none(), _CURRENCY),
        amount_minor=_AMOUNT,
    ),
    st.builds(Refund, pick=st.integers(min_value=0, max_value=50), permille=st.integers(1, 1000)),
    st.builds(
        Payout,
        user=_USER,
        method=st.sampled_from(PAYOUT_METHODS),
        currency=_CURRENCY,
        amount_minor=_AMOUNT,
        answer=st.sampled_from(("succeeded", "failed", "unknown", "timeout")),
    ),
    st.builds(
        Settle,
        pick=st.integers(min_value=0, max_value=50),
        answer=st.sampled_from(("succeeded", "failed", "unknown")),
    ),
)


# --- harness -------------------------------------------------------------------------------------------


class ScriptedRail:
    """A rail that answers whatever the test last scripted; `timeout` raises like a lost connection."""

    name = "scripted"

    def __init__(self) -> None:
        self.answer: RailAnswer = "succeeded"

    def submit(
        self, payout_id: str, method: str, amount_minor: int, currency: str, destination: str
    ) -> RailResult:
        answer = self.answer
        if answer == "timeout":
            raise TimeoutError("scripted rail timeout")
        if answer == "failed":
            return RailResult("failed", failure_code="scripted_failure")
        if answer == "unknown":
            return RailResult("unknown")
        return RailResult("succeeded")


def _reset_state(engine: Engine) -> None:
    with engine.begin() as conn:
        tables = (
            conn.execute(
                text(
                    "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                    "WHERE schemaname = ANY(:schemas) AND NOT (tablename = ANY(:kept))"
                ),
                {"schemas": list(RESET_SCHEMAS), "kept": list(KEPT_TABLES)},
            )
            .scalars()
            .all()
        )
        conn.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))


@dataclasses.dataclass(frozen=True)
class _PaymentRecord:
    payment_id: uuid.UUID
    amount_minor: int


@dataclasses.dataclass(frozen=True)
class _PayoutRecord:
    payout_id: uuid.UUID
    currency: str
    amount_minor: int


class World:
    """Users, one merchant and the services under test, plus a model of what must be true afterwards."""

    def __init__(self, session_factory: sessionmaker[Session], clock: FixedClock, user_count: int) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.user_ids: list[uuid.UUID] = []
        self.merchant_id = uuid.uuid4()
        with session_factory() as session:
            for index in range(user_count):
                self.user_ids.append(sign_up(session, clock, f"user{index}@example.com", PASSWORD).user_id)
            session.add(Merchant(id=self.merchant_id, name="Acme", created_at=clock()))
            session.commit()
        self.rail = ScriptedRail()
        self.simulator = SimulatorProvider(session_factory, clock)
        self.charge_deps = ChargeDeps(
            session_factory=session_factory,
            redis=None,
            provider=self.simulator,
            clock=clock,
            card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
        )
        self.payout_deps = PayoutDeps(session_factory=session_factory, clock=clock, rail=self.rail)
        self.succeeded_payments: list[_PaymentRecord] = []
        self.payouts: list[_PayoutRecord] = []
        self.payout_state: dict[uuid.UUID, str] = {}
        self._keys = 0

    def _key(self, prefix: str) -> str:
        self._keys += 1
        return f"{prefix}-{self._keys}"

    def _user(self, index: int) -> uuid.UUID:
        return self.user_ids[index % len(self.user_ids)]

    def apply(self, step: Step) -> None:
        if isinstance(step, TopUp):
            with self.session_factory.begin() as session:
                top_up_wallet(session, self.clock, self._user(step.user), step.currency, step.amount_minor)
        elif isinstance(step, Transfer):
            self._transfer(step)
        elif isinstance(step, Charge):
            self._charge(step)
        elif isinstance(step, Refund):
            self._refund(step)
        elif isinstance(step, Payout):
            self._payout(step)
        else:
            self._settle(step)

    def _transfer(self, step: Transfer) -> None:
        users = len(self.user_ids)
        recipient_index = (step.sender % users + 1 + (step.offset - 1) % (users - 1)) % users
        outcome = transfer(
            self.session_factory,
            self.clock,
            sender_user_id=self._user(step.sender),
            idempotency_key=self._key("transfer"),
            recipient_email=f"user{recipient_index}@example.com",
            amount_minor=step.amount_minor,
            currency=step.currency,
            recipient_currency=step.recipient_currency,
        )
        assert outcome.status_code in (201, 409, 422)
        if outcome.status_code == 409:
            assert outcome.body["error"] == "insufficient_funds"
        if outcome.status_code == 422:
            assert outcome.body == {"error": "fx_amount_too_small"}

    def _charge(self, step: Charge) -> None:
        payload: dict[str, object] = {
            "amount_minor": step.amount_minor,
            "currency": step.currency,
            "method": "card",
            "card": {
                "number": APPROVE_PAN if step.approve else DECLINE_PAN,
                "exp_month": 12,
                "exp_year": 2030,
                "cvc": "123",
            },
        }
        if step.settlement_currency is not None:
            payload["settlement_currency"] = step.settlement_currency
        outcome = charge_card(
            self.charge_deps,
            self.merchant_id,
            self._key("charge"),
            ChargeRequest.model_validate(payload),
        )
        assert outcome.status_code in (201, 422)
        if outcome.status_code == 422:
            assert outcome.body == {"error": "fx_amount_too_small"}
            return
        assert outcome.body["state"] == ("succeeded" if step.approve else "declined")
        if step.approve:
            self.succeeded_payments.append(
                _PaymentRecord(uuid.UUID(str(outcome.body["payment_id"])), step.amount_minor)
            )

    def _refund(self, step: Refund) -> None:
        if not self.succeeded_payments:
            return
        payment = self.succeeded_payments[step.pick % len(self.succeeded_payments)]
        amount_minor = max(1, payment.amount_minor * step.permille // 1000)
        outcome = refund_payment(
            self.session_factory,
            self.clock,
            self.merchant_id,
            payment.payment_id,
            self._key("refund"),
            amount_minor,
        )
        assert outcome.status_code in (201, 409)
        if outcome.status_code == 409:
            assert outcome.body["error"] == "refund_exceeds_refundable"

    def _payout(self, step: Payout) -> None:
        self.rail.answer = step.answer
        outcome = create_payout(
            self.payout_deps,
            self._user(step.user),
            self._key("payout"),
            step.method,
            step.amount_minor,
            step.currency,
            DESTINATION,
        )
        if outcome.status_code == 409:
            assert outcome.body["error"] == "insufficient_funds"
            return
        expected_state = PAYOUT_STATE_BY_ANSWER[step.answer]
        assert outcome.status_code == PAYOUT_STATUS_BY_STATE[expected_state]
        assert outcome.body["state"] == expected_state
        payout_id = uuid.UUID(str(outcome.body["payout_id"]))
        self.payouts.append(_PayoutRecord(payout_id, step.currency, step.amount_minor))
        self.payout_state[payout_id] = expected_state

    def _settle(self, step: Settle) -> None:
        if not self.payouts:
            return
        payout = self.payouts[step.pick % len(self.payouts)]
        view = settle_payout(self.session_factory, self.clock, payout.payout_id, RailResult(step.answer))
        current = self.payout_state[payout.payout_id]
        if current not in ("succeeded", "failed"):
            self.payout_state[payout.payout_id] = step.answer
        assert view.state == self.payout_state[payout.payout_id]


# --- invariants ----------------------------------------------------------------------------------------


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _raw_wallet_balances(
    session_factory: sessionmaker[Session], user_id: uuid.UUID
) -> dict[str, tuple[int, int]]:
    """currency -> (available, held) from the raw sums of the postings on the wallet and hold accounts."""
    rows = _rows(
        session_factory,
        "SELECT a.currency, a.kind, COALESCE(SUM(p.amount_minor), 0)::bigint AS posted "
        "FROM gateway.ledger_accounts a LEFT JOIN gateway.postings p ON p.account_id = a.id "
        "WHERE a.owner_type = 'user' AND a.owner_id = :user_id AND a.kind IN (:wallet, :hold) "
        "GROUP BY a.currency, a.kind",
        user_id=user_id,
        wallet=WALLET_KIND,
        hold=HOLD_KIND,
    )
    posted = {(str(row["currency"]), str(row["kind"])): int(str(row["posted"])) for row in rows}
    return {
        currency: (-posted[(currency, WALLET_KIND)], -posted.get((currency, HOLD_KIND), 0))
        for currency, kind in posted
        if kind == WALLET_KIND
    }


def assert_ledger_invariants(world: World) -> None:
    session_factory = world.session_factory
    unbalanced = _rows(
        session_factory,
        "SELECT journal_id, currency, sum(amount_minor)::bigint AS total FROM gateway.postings "
        "GROUP BY journal_id, currency HAVING sum(amount_minor) <> 0",
    )
    assert unbalanced == []

    fx_mismatch = _rows(
        session_factory,
        "SELECT j.id, j.ref_type, j.fx_rate_id, count(DISTINCT p.currency) AS currencies "
        "FROM gateway.journal_entries j JOIN gateway.postings p ON p.journal_id = j.id "
        "GROUP BY j.id, j.ref_type, j.fx_rate_id "
        "HAVING (count(DISTINCT p.currency) > 1) <> (j.fx_rate_id IS NOT NULL)",
    )
    assert fx_mismatch == []

    for user_id in world.user_ids:
        with session_factory() as session:
            wallets = list_wallets(session, user_id)
            from_postings = ledger_wallet_balances(session, user_id)
        assert wallets == from_postings
        assert {w.currency: (w.available_minor, w.held_minor) for w in wallets} == _raw_wallet_balances(
            session_factory, user_id
        )
        assert [w for w in wallets if w.available_minor < 0 or w.held_minor < 0] == []

    _assert_payouts_match_the_ledger(world)


def _assert_payouts_match_the_ledger(world: World) -> None:
    session_factory = world.session_factory
    stored = {
        uuid.UUID(str(row["id"])): str(row["state"])
        for row in _rows(session_factory, "SELECT id, state FROM gateway.payouts")
    }
    assert stored == world.payout_state

    journal_counts = {
        str(row["ref_type"]): int(str(row["n"]))
        for row in _rows(
            session_factory,
            "SELECT ref_type, count(*) AS n FROM gateway.journal_entries "
            "WHERE ref_type LIKE 'payout\\_%' GROUP BY ref_type",
        )
    }
    states = list(world.payout_state.values())
    expected_counts = {
        "payout_hold": len(states),
        "payout_capture": states.count("succeeded"),
        "payout_release": states.count("failed"),
    }
    assert journal_counts == {ref_type: n for ref_type, n in expected_counts.items() if n > 0}

    captured: dict[str, int] = {}
    for payout in world.payouts:
        if world.payout_state[payout.payout_id] == "succeeded":
            captured[payout.currency] = captured.get(payout.currency, 0) + payout.amount_minor
    clearing = {
        str(row["currency"]): int(str(row["total"]))
        for row in _rows(
            session_factory,
            "SELECT p.currency, sum(p.amount_minor)::bigint AS total FROM gateway.postings p "
            "JOIN gateway.ledger_accounts a ON a.id = p.account_id "
            "WHERE a.owner_type = 'platform' AND a.kind = 'payout_clearing' GROUP BY p.currency",
        )
    }
    assert clearing == {currency: -total for currency, total in captured.items()}


# --- tests ---------------------------------------------------------------------------------------------


@PROPERTY_SETTINGS
@given(user_count=st.integers(min_value=2, max_value=3), steps=st.lists(STEPS, max_size=12))
def test_property_random_operation_sequences_keep_the_ledger_balanced_and_wallets_equal_to_postings(
    session_factory: sessionmaker[Session],
    engine: Engine,
    clock: FixedClock,
    user_count: int,
    steps: list[Step],
) -> None:
    # Arrange
    _reset_state(engine)
    world = World(session_factory, clock, user_count)

    # Act / Assert
    for step in steps:
        world.apply(step)
        assert_ledger_invariants(world)


def test_a_fixed_mixed_sequence_keeps_the_invariants_and_ends_with_the_expected_balances(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    world = World(session_factory, clock, 2)
    steps: list[Step] = [
        TopUp(user=0, currency="USD", amount_minor=50_000),
        Transfer(sender=0, offset=1, currency="USD", recipient_currency="EUR", amount_minor=10_000),
        Charge(approve=True, currency="USD", settlement_currency="JPY", amount_minor=20_000),
        Charge(approve=False, currency="EUR", settlement_currency="USD", amount_minor=700),
        Refund(pick=0, permille=500),
        Payout(user=1, method="bank_transfer", currency="EUR", amount_minor=3_000, answer="unknown"),
        Payout(user=0, method="upi", currency="USD", amount_minor=5_000, answer="succeeded"),
        Settle(pick=0, answer="failed"),
        Settle(pick=1, answer="failed"),
    ]

    # Act
    for step in steps:
        world.apply(step)
        assert_ledger_invariants(world)

    # Assert
    with session_factory() as session:
        sender = [
            (w.currency, w.available_minor, w.held_minor) for w in list_wallets(session, world.user_ids[0])
        ]
        recipient = [
            (w.currency, w.available_minor, w.held_minor) for w in list_wallets(session, world.user_ids[1])
        ]
    assert sender == [("USD", 35_000, 0)]
    assert recipient == [("EUR", 9_200, 0), ("USD", 0, 0)]
    assert world.payout_state == {
        world.payouts[0].payout_id: "failed",
        world.payouts[1].payout_id: "succeeded",
    }


def test_settling_a_succeeded_payout_again_with_a_contradictory_answer_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    world = World(session_factory, clock, 2)
    for step in (
        TopUp(user=0, currency="USD", amount_minor=10_000),
        Payout(user=0, method="bank_transfer", currency="USD", amount_minor=4_000, answer="succeeded"),
    ):
        world.apply(step)

    # Act
    world.apply(Settle(pick=0, answer="failed"))
    world.apply(Settle(pick=0, answer="succeeded"))

    # Assert
    assert_ledger_invariants(world)
    with session_factory() as session:
        wallets = [
            (w.currency, w.available_minor, w.held_minor) for w in list_wallets(session, world.user_ids[0])
        ]
    assert wallets == [("USD", 6_000, 0)]
    assert world.payout_state == {world.payouts[0].payout_id: "succeeded"}
