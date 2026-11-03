"""RED tests for the B7.1 security-audit / red-team hardening, from docs/specs/gateway-basic.md "## B7.1".

- RT-001 / MEDIUM-1 (Decision 4): concurrent charges on one card are scored one after another.
- LOW-3 (Decision 3): a blocked charge is settled in tx1: no attempt row, no provider attribution.
- LOW-2: risk-blocked payments are not declines for `recent_declines`.
- MEDIUM-2 (Decision 5): the payment stores the card fingerprint itself.
- LOW-4: `get_payment_risk` reads the `risk_blocked` column, not the decline code.
- LOW-6: the fingerprint key stays out of `repr`.

The crash window between the decision and its record cannot be driven from the outside: `ChargeHooks` only
has `after_tx2_commit`, nothing fires between tx1 and the provider call, so it is covered by the structural
checks (no attempt row, the stored body, provider never called).
"""

import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, fields
from datetime import timedelta

import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, ChargeOutcome, _Claim, charge_card
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.risk import get_payment_risk
from gateway.payments.schemas import ChargeRequest, PaymentRiskView
from gateway.payments.tokenization.cards import card_fingerprint
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
HIGH_USD = 500_000
PARALLEL_CHARGES = 8
RACE_ROUNDS = 5


class CountingProvider:
    """Wraps a provider and counts `charge` calls (thread-safe)."""

    def __init__(self, inner: ProviderPort) -> None:
        self.name = inner.name
        self._inner = inner
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    provider: CountingProvider
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID


def _harness(session_factory: sessionmaker[Session], clock: FixedClock) -> Harness:
    provider = CountingProvider(SimulatorProvider(session_factory, clock))
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=provider,
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(deps, provider, session_factory, clock, credentials.merchant_id)


def _request(pan: str, amount_minor: int) -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _charge(
    h: Harness, pan: str = APPROVE_PAN, amount_minor: int = 1500, key: str | None = None
) -> ChargeOutcome:
    return charge_card(h.deps, h.merchant_id, key or uuid.uuid4().hex, _request(pan, amount_minor))


def _payment_id(outcome: ChargeOutcome) -> uuid.UUID:
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _prime(h: Harness, pan: str, count: int) -> list[uuid.UUID]:
    """`count` small charges one minute apart; the clock ends `count` minutes after it started."""
    ids: list[uuid.UUID] = []
    for _ in range(count):
        ids.append(_payment_id(_charge(h, pan=pan)))
        h.clock.advance(timedelta(minutes=1))
    return ids


def _blocked_charge(h: Harness, key: str = "key-blocked") -> ChargeOutcome:
    """Three small declines make velocity + recent_declines hit; a high amount adds the third rule."""
    _prime(h, DECLINED_PAN, 3)
    return _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD, key=key)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], query: str) -> int:
    with session_factory() as session:
        return int(session.execute(text(query)).scalar_one())


def _charge_in_parallel(h: Harness, pan: str, amount_minor: int) -> list[ChargeOutcome]:
    """Releases `PARALLEL_CHARGES` charges (distinct idempotency keys) at the same instant."""
    barrier = threading.Barrier(PARALLEL_CHARGES)

    def worker(index: int) -> ChargeOutcome:
        barrier.wait(timeout=30)
        return _charge(h, pan=pan, amount_minor=amount_minor, key=f"parallel-{index}")

    with ThreadPoolExecutor(max_workers=PARALLEL_CHARGES) as pool:
        futures = [pool.submit(worker, index) for index in range(PARALLEL_CHARGES)]
        return [future.result(timeout=60) for future in futures]


def _reasons(value: object) -> tuple[str, ...]:
    assert isinstance(value, list)
    return tuple(str(reason) for reason in value)


def _assessments_of_amount(
    session_factory: sessionmaker[Session], amount_minor: int
) -> Counter[tuple[str, tuple[str, ...], bool, bool, str | None]]:
    rows = _rows(
        session_factory,
        "SELECT risk_score, risk_reasons, risk_flagged, risk_blocked, decline_code "
        "FROM gateway.payments WHERE amount_minor = :amount",
        amount=amount_minor,
    )
    return Counter(
        (
            str(row["risk_score"]),
            _reasons(row["risk_reasons"]),
            bool(row["risk_flagged"]),
            bool(row["risk_blocked"]),
            None if row["decline_code"] is None else str(row["decline_code"]),
        )
        for row in rows
    )


# --- RT-001: concurrent charges on one card are scored serially (Decision 4) -------------------------------


@pytest.mark.parametrize("round_number", range(RACE_ROUNDS))
def test_parallel_high_amount_charges_on_a_card_with_two_declines_reach_the_provider_at_most_once(
    session_factory: sessionmaker[Session], clock: FixedClock, round_number: int
) -> None:
    # Arrange: two provider declines => recent_declines; the first scored charge sees 2 earlier payments (no
    # velocity: 0.40 + 0.30 = 0.70, flagged, goes to the provider); every later one sees at least 3 => blocked
    h = _harness(session_factory, clock)
    _prime(h, DECLINED_PAN, 2)
    calls_before = h.provider.calls

    # Act
    outcomes = _charge_in_parallel(h, DECLINED_PAN, HIGH_USD)

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * PARALLEL_CHARGES
    assert Counter((outcome.body["state"], outcome.body["decline_code"]) for outcome in outcomes) == {
        ("declined", "card_declined"): 1,
        ("declined", "risk_blocked"): PARALLEL_CHARGES - 1,
    }
    assert h.provider.calls - calls_before == 1
    assert _assessments_of_amount(session_factory, HIGH_USD) == {
        ("0.70", ("high_amount", "recent_declines"), True, False, "card_declined"): 1,
        ("1.00", ("high_amount", "card_velocity", "recent_declines"), True, True, "risk_blocked"): (
            PARALLEL_CHARGES - 1
        ),
    }
    assert _count(session_factory, "SELECT count(*) FROM gateway.payment_attempts") == 3


@pytest.mark.parametrize("round_number", range(RACE_ROUNDS))
def test_parallel_charges_on_a_clean_card_see_each_other_so_velocity_hits_exactly_as_in_a_sequence(
    session_factory: sessionmaker[Session], clock: FixedClock, round_number: int
) -> None:
    # Arrange: serially the earlier-payment counts are 0..7, so three charges see fewer than 3 and five see 3+
    h = _harness(session_factory, clock)

    # Act
    outcomes = _charge_in_parallel(h, APPROVE_PAN, HIGH_USD)

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * PARALLEL_CHARGES
    assert Counter(str(outcome.body["state"]) for outcome in outcomes) == {"succeeded": PARALLEL_CHARGES}
    assert h.provider.calls == PARALLEL_CHARGES
    assert _assessments_of_amount(session_factory, HIGH_USD) == {
        ("0.40", ("high_amount",), False, False, None): 3,
        ("0.80", ("high_amount", "card_velocity"), True, False, None): PARALLEL_CHARGES - 3,
    }


# --- Decision 3: a blocked charge is settled in tx1 ------------------------------------------------------


def test_blocked_charge_has_no_payment_attempt_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    payment_id = _payment_id(outcome)
    attempts = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.payment_attempts WHERE payment_id = :id",
        id=payment_id,
    )
    assert attempts == [{"n": 0}]
    assert _count(session_factory, "SELECT count(*) FROM gateway.payment_attempts") == 3


def test_blocked_charge_is_stored_declined_with_risk_blocked_true_and_never_reaches_the_provider(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, DECLINED_PAN, 3)
    simulator_charges_before = _count(session_factory, "SELECT count(*) FROM simulator.charges")

    # Act
    outcome = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT state, decline_code, risk_blocked, risk_flagged, entity_version "
        "FROM gateway.payments WHERE id = :id",
        id=_payment_id(outcome),
    )
    assert rows == [
        {
            "state": "declined",
            "decline_code": "risk_blocked",
            "risk_blocked": True,
            "risk_flagged": True,
            "entity_version": 1,
        }
    ]
    assert h.provider.calls == 3
    assert _count(session_factory, "SELECT count(*) FROM simulator.charges") == simulator_charges_before


def test_blocked_charge_outbox_event_has_no_provider_and_the_risk_blocked_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT topic, payload FROM gateway.outbox WHERE payload->>'payment_id' = :pid",
        pid=str(_payment_id(outcome)),
    )
    assert len(rows) == 1
    assert rows[0]["topic"] == PAYMENTS_TOPIC
    payload = rows[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["provider"] is None
    assert payload["decline_code"] == "risk_blocked"
    event = PaymentEventV1.model_validate(payload)
    assert (event.event_type, event.state, event.provider, event.decline_code) == (
        "payment.declined",
        "declined",
        None,
        "risk_blocked",
    )


def test_blocked_charge_audit_summary_carries_the_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT action, summary FROM gateway.audit_log WHERE target = :target",
        target=f"payment:{_payment_id(outcome)}",
    )
    assert rows == [
        {
            "action": "payment.charge",
            "summary": {
                "state": "declined",
                "amount_minor": HIGH_USD,
                "currency": "USD",
                "decline_code": "risk_blocked",
            },
        }
    ]


def test_blocked_charge_replay_returns_the_stored_body_without_a_new_attempt_or_provider_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    first = _blocked_charge(h, key="key-replay")
    calls_before = h.provider.calls
    attempts_before = _count(session_factory, "SELECT count(*) FROM gateway.payment_attempts")

    # Act
    replay = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD, key="key-replay")

    # Assert
    assert replay == first
    assert h.provider.calls == calls_before
    assert _count(session_factory, "SELECT count(*) FROM gateway.payment_attempts") == attempts_before


@pytest.mark.parametrize(
    ("pan", "amount_minor", "prime_pan", "prime_count"),
    [
        (APPROVE_PAN, 1500, APPROVE_PAN, 0),
        (APPROVE_PAN, HIGH_USD, APPROVE_PAN, 3),
        (DECLINED_PAN, 1500, DECLINED_PAN, 3),
    ],
    ids=["clean", "flagged_not_blocked", "provider_decline_not_blocked"],
)
def test_a_charge_that_is_not_blocked_stores_risk_blocked_false(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    pan: str,
    amount_minor: int,
    prime_pan: str,
    prime_count: int,
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _prime(h, prime_pan, prime_count)

    # Act
    outcome = _charge(h, pan=pan, amount_minor=amount_minor)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT risk_blocked FROM gateway.payments WHERE id = :id",
        id=_payment_id(outcome),
    )
    assert rows == [{"risk_blocked": False}]


# --- LOW-2: a block must not extend itself ---------------------------------------------------------------


def test_risk_blocked_payments_still_count_for_velocity_but_not_for_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: three declined payments marked as blocks => velocity yes (3 in 10 minutes), declines no
    h = _harness(session_factory, clock)
    prior = _prime(h, DECLINED_PAN, 3)
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE gateway.payments "
                "SET risk_blocked = true, decline_code = 'risk_blocked' WHERE id = ANY(:ids)"
            ),
            {"ids": prior},
        )
        session.commit()
    calls_before = h.provider.calls

    # Act
    outcome = _charge(h, pan=DECLINED_PAN, amount_minor=HIGH_USD)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT state, decline_code, risk_score, risk_reasons, risk_flagged, risk_blocked "
        "FROM gateway.payments WHERE id = :id",
        id=_payment_id(outcome),
    )
    assert [
        (str(row["risk_score"]), row["risk_reasons"], row["risk_flagged"], row["risk_blocked"])
        for row in rows
    ] == [("0.80", ["high_amount", "card_velocity"], True, False)]
    assert [(row["state"], row["decline_code"]) for row in rows] == [("declined", "card_declined")]
    assert h.provider.calls == calls_before + 1


# --- Decision 5: the payment stores the card fingerprint -------------------------------------------------


@pytest.mark.parametrize("pan", [APPROVE_PAN, DECLINED_PAN], ids=["approved_card", "declined_card"])
def test_payment_stores_the_fingerprint_of_its_card_token(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _charge(h, pan=pan)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT p.card_fingerprint AS stored, t.fingerprint AS token_fingerprint "
        "FROM gateway.payments p JOIN gateway.card_tokens t ON t.token = p.card_token WHERE p.id = :id",
        id=_payment_id(outcome),
    )
    expected = card_fingerprint(pan, TEST_CARD_FINGERPRINT_KEY)
    assert rows == [{"stored": expected, "token_fingerprint": expected}]


def test_blocked_payment_also_stores_the_card_fingerprint(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    outcome = _blocked_charge(h)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT card_fingerprint FROM gateway.payments WHERE id = :id",
        id=_payment_id(outcome),
    )
    assert rows == [{"card_fingerprint": card_fingerprint(DECLINED_PAN, TEST_CARD_FINGERPRINT_KEY)}]


# --- LOW-4: get_payment_risk reads the column, not the decline code --------------------------------------


def test_get_payment_risk_ignores_a_decline_code_literally_named_risk_blocked_when_the_column_is_false(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h, pan=DECLINED_PAN))
    with session_factory() as session:
        session.execute(
            text("UPDATE gateway.payments SET decline_code = 'risk_blocked' WHERE id = :id"),
            {"id": payment_id},
        )
        session.commit()

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=False, blocked=False)


def test_get_payment_risk_reports_blocked_when_the_column_is_true(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _payment_id(_charge(h))
    with session_factory() as session:
        session.execute(
            text("UPDATE gateway.payments SET risk_blocked = true WHERE id = :id"), {"id": payment_id}
        )
        session.commit()

    # Act
    with session_factory() as session:
        view = get_payment_risk(session, h.merchant_id, payment_id)

    # Assert
    assert view == PaymentRiskView(payment_id=payment_id, risk_flagged=False, blocked=True)


# --- LOW-6: the fingerprint key stays out of repr --------------------------------------------------------


def test_charge_deps_repr_does_not_contain_the_fingerprint_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    rendered = repr(h.deps)

    # Assert
    assert TEST_CARD_FINGERPRINT_KEY not in rendered
    assert "card_fingerprint_key" not in rendered


def test_charge_deps_and_claim_exclude_the_fingerprint_fields_from_repr() -> None:
    # Arrange / Act
    in_repr = {
        (cls.__name__, field.name): field.repr
        for cls in (ChargeDeps, _Claim)
        for field in fields(cls)
        if field.name in {"card_fingerprint_key", "card_fingerprint"}
    }

    # Assert
    assert in_repr == {("ChargeDeps", "card_fingerprint_key"): False, ("_Claim", "card_fingerprint"): False}
