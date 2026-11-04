"""Tests for the in-flight sibling rule of `recent_declines`, written from docs/specs/gateway-basic.md B7.1
Decision 7 (red-team R-1).

`recent_declines` also counts the card's earlier payments in state `created` whose `created_at` is within
`IN_FLIGHT_WINDOW` (= `timedelta(seconds=ATTEMPT_LEASE_SECONDS)`, inclusive of the window start) of `now`.
Older `created` rows, `unknown` and `failed` rows stay uncounted. Prior payments are seeded straight through
the session (as in test_risk.py); the charge-level burst drives `charge_card` as test_charge_risk_hardening.py
does.
"""

import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from gateway.accounts.models import Merchant
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ATTEMPT_LEASE_SECONDS, ChargeDeps, ChargeOutcome, charge_card
from gateway.payments.models import Payment
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.risk import (
    DECLINE_LIMIT,
    IN_FLIGHT_WINDOW,
    VELOCITY_LIMIT,
    RiskAssessment,
    assess_charge_risk,
)
from gateway.payments.schemas import ChargeRequest
from gateway.payments.tokenization.models import CardToken
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64
SMALL_AMOUNT = 1500
ONE_SECOND = timedelta(seconds=1)
THIRTY_MINUTES = timedelta(minutes=30)
FIVE_MINUTES = timedelta(minutes=5)
FIVE_SECONDS = timedelta(seconds=5)

NO_RISK = RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)
DECLINES_ONLY = RiskAssessment(
    score=Decimal("0.30"), reasons=("recent_declines",), flagged=False, blocked=False
)
VELOCITY_ONLY = RiskAssessment(
    score=Decimal("0.40"), reasons=("card_velocity",), flagged=False, blocked=False
)

DECLINED_PAN = "4000000000000002"
HIGH_USD = 500_000
BURST_SIZE = 8
BURST_ROUNDS = 3
PROVIDER_DELAY_SECONDS = 0.5


def _seed_merchant(session: Session, now: datetime, name: str = "Acme") -> uuid.UUID:
    merchant_id = uuid.uuid4()
    session.add(Merchant(id=merchant_id, name=name, created_at=now))
    session.flush()
    return merchant_id


def _seed_payment(
    session: Session,
    merchant_id: uuid.UUID,
    created_at: datetime,
    state: str,
    fingerprint: str = FINGERPRINT,
    risk_blocked: bool = False,
) -> None:
    token = f"tok_{uuid.uuid4().hex}"
    payment_id = uuid.uuid4()
    session.add(
        CardToken(
            token=token,
            fingerprint=fingerprint,
            bin="424242",
            last4="4242",
            exp_month=12,
            exp_year=2030,
            created_at=created_at,
        )
    )
    session.flush()  # payments.card_token has an FK but no relationship(), so order the inserts by hand
    session.add(
        Payment(
            id=payment_id,
            merchant_id=merchant_id,
            amount_minor=SMALL_AMOUNT,
            currency="USD",
            method="card",
            state=state,
            card_token=token,
            entity_version=1,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    session.flush()
    session.execute(
        text("UPDATE gateway.payments SET card_fingerprint = :fp, risk_blocked = :blocked WHERE id = :id"),
        {"fp": fingerprint, "blocked": risk_blocked, "id": payment_id},
    )


def _seed_many(
    session: Session,
    merchant_id: uuid.UUID,
    created_at: datetime,
    count: int,
    state: str,
    fingerprint: str = FINGERPRINT,
) -> None:
    for _ in range(count):
        _seed_payment(session, merchant_id, created_at, state, fingerprint)


def _assess(session: Session, now: datetime, fingerprint: str = FINGERPRINT) -> RiskAssessment:
    return assess_charge_risk(session, now, fingerprint, SMALL_AMOUNT, "USD")


# --- constant -------------------------------------------------------------------------------------------


def test_in_flight_window_is_the_attempt_lease_and_thirty_seconds() -> None:
    # Arrange / Act
    windows = (IN_FLIGHT_WINDOW, timedelta(seconds=ATTEMPT_LEASE_SECONDS))

    # Assert
    assert windows == (timedelta(seconds=30), timedelta(seconds=30))


# --- recent_declines counts in-flight created siblings ----------------------------------------------------


def test_assess_decline_limit_created_rows_inside_the_in_flight_window_hit_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - FIVE_SECONDS, DECLINE_LIMIT, state="created")
        result = _assess(session, now)

    # Assert
    assert result == DECLINES_ONLY
    assert str(result.score) == "0.30"


def test_assess_one_created_row_inside_the_in_flight_window_is_below_the_decline_limit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - FIVE_SECONDS, DECLINE_LIMIT - 1, state="created")
        result = _assess(session, now)

    # Assert
    assert result == NO_RISK


def test_assess_one_declined_and_one_in_flight_created_count_as_two_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - THIRTY_MINUTES, state="declined")
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="created")
        result = _assess(session, now)

    # Assert
    assert result == DECLINES_ONLY


def test_assess_a_risk_blocked_decline_plus_one_in_flight_created_is_still_below_the_decline_limit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: the block is not a decline (audit LOW-2), so only the created row counts
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - THIRTY_MINUTES, state="declined", risk_blocked=True)
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="created")
        result = _assess(session, now)

    # Assert
    assert result == NO_RISK


def test_assess_in_flight_created_rows_of_other_merchants_on_the_same_card_count(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        first_merchant = _seed_merchant(session, now, "Acme")
        second_merchant = _seed_merchant(session, now, "Globex")
        _seed_payment(session, first_merchant, now - FIVE_SECONDS, state="created")
        _seed_payment(session, second_merchant, now - FIVE_SECONDS, state="created")
        result = _assess(session, now)

    # Assert
    assert result == DECLINES_ONLY


def test_assess_in_flight_created_rows_add_to_velocity_and_decline_weights_without_double_counting(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: three in-flight rows = 3 in the velocity window and 3 against the decline limit
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - FIVE_SECONDS, VELOCITY_LIMIT, state="created")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.70"),
        reasons=("card_velocity", "recent_declines"),
        flagged=True,
        blocked=False,
    )


# --- boundaries of IN_FLIGHT_WINDOW -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "age",
    [timedelta(0), ONE_SECOND, IN_FLIGHT_WINDOW - ONE_SECOND, IN_FLIGHT_WINDOW],
    ids=["age_0s", "age_1s", "age_29s", "age_exactly_the_window"],
)
def test_assess_a_created_row_inside_or_exactly_at_the_window_start_counts(
    session_factory: sessionmaker[Session], clock: FixedClock, age: timedelta
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - THIRTY_MINUTES, state="declined")
        _seed_payment(session, merchant_id, now - age, state="created")
        result = _assess(session, now)

    # Assert
    assert result == DECLINES_ONLY


@pytest.mark.parametrize(
    "age",
    [IN_FLIGHT_WINDOW + ONE_SECOND, FIVE_MINUTES],
    ids=["one_second_older_than_the_window", "five_minutes"],
)
def test_assess_a_created_row_older_than_the_window_does_not_count(
    session_factory: sessionmaker[Session], clock: FixedClock, age: timedelta
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - THIRTY_MINUTES, state="declined")
        _seed_payment(session, merchant_id, now - age, state="created")
        result = _assess(session, now)

    # Assert
    assert result == NO_RISK


# --- states that stay uncounted ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "age"),
    [("created", FIVE_MINUTES), ("unknown", FIVE_SECONDS), ("failed", FIVE_SECONDS)],
    ids=["stale_created", "unknown", "failed"],
)
def test_assess_stale_created_unknown_and_failed_rows_do_not_count_for_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock, state: str, age: timedelta
) -> None:
    # Arrange: DECLINE_LIMIT rows (2 < VELOCITY_LIMIT) so nothing else fires either
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - age, DECLINE_LIMIT, state=state)
        result = _assess(session, now)

    # Assert
    assert result == NO_RISK


@pytest.mark.parametrize(
    ("state", "age"),
    [("created", FIVE_MINUTES), ("unknown", FIVE_SECONDS), ("failed", FIVE_SECONDS)],
    ids=["stale_created", "unknown", "failed"],
)
def test_assess_stale_created_unknown_and_failed_rows_still_count_for_card_velocity_only(
    session_factory: sessionmaker[Session], clock: FixedClock, state: str, age: timedelta
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - age, VELOCITY_LIMIT, state=state)
        result = _assess(session, now)

    # Assert
    assert result == VELOCITY_ONLY


def test_assess_only_the_in_flight_created_row_of_a_mixed_history_counts_for_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: one in-flight created (counts), one stale created, one unknown, one failed (do not) => 1
    # decline; all four are inside the 10-minute velocity window => card_velocity
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="created")
        _seed_payment(session, merchant_id, now - FIVE_MINUTES, state="created")
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="unknown")
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="failed")
        result = _assess(session, now)

    # Assert
    assert result == VELOCITY_ONLY


def test_assess_a_stale_created_row_beside_one_in_flight_created_row_is_one_decline_not_two(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: the stale row is 31 minutes old, outside the velocity window as well
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - THIRTY_MINUTES - ONE_SECOND, state="created")
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="created")
        result = _assess(session, now)

    # Assert
    assert result == NO_RISK


# --- other fingerprints -----------------------------------------------------------------------------------


def test_assess_in_flight_created_rows_of_other_fingerprints_are_ignored(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: one own in-flight row, four of another card => the own card is below the decline limit
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - FIVE_SECONDS, state="created")
        _seed_many(
            session, merchant_id, now - FIVE_SECONDS, 4, state="created", fingerprint=OTHER_FINGERPRINT
        )
        result = _assess(session, now)

    # Assert
    assert result == NO_RISK


# --- charge level: a parallel burst on one card (R-1) -----------------------------------------------------
#
# Expected numbers (HIGH_USD on a new card the provider declines; the advisory lock serialises tx1, so the
# k-th scored charge (k = 0..7) sees exactly k earlier payments; only provider-bound ones are `created` or
# provider-declined and count as declines, the blocked ones are risk_blocked and do not):
#   k=0: high_amount                                   = 0.40 -> provider (1 decline-or-in-flight after)
#   k=1: 1 earlier (< DECLINE_LIMIT)                   = 0.40 -> provider (2 after)
#   k=2: 2 earlier => recent_declines                  = 0.40 + 0.30 = 0.70, flagged -> provider (3 after)
#   k=3..7: 3+ earlier => card_velocity (+0.40) and recent_declines (3 counted, +0.30) = 1.10, capped 1.00
#           >= BLOCK_THRESHOLD 0.90 -> risk_blocked, never reaches the provider
# So 3 provider calls and 5 blocks for a burst of 8, whether the charges are parallel (in-flight siblings
# count) or sequential (provider-declined siblings count). Before Decision 7 the parallel burst reached the
# provider 8 times.

EXPECTED_PROVIDER_CALLS = 3
EXPECTED_BLOCKED = BURST_SIZE - EXPECTED_PROVIDER_CALLS


class SlowCountingProvider:
    """Counts `charge` calls (thread-safe) and answers only after a delay, so siblings stay `created`."""

    def __init__(self, inner: ProviderPort, delay_seconds: float) -> None:
        self.name = inner.name
        self._inner = inner
        self._delay_seconds = delay_seconds
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        with self._lock:
            return self._calls

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        with self._lock:
            self._calls += 1
        time.sleep(self._delay_seconds)
        return self._inner.charge(provider_key, amount_minor, currency, card)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return self._inner.lookup(provider_key)


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    provider: SlowCountingProvider
    session_factory: sessionmaker[Session]
    merchant_id: uuid.UUID


def _harness(session_factory: sessionmaker[Session], clock: FixedClock, delay_seconds: float) -> Harness:
    provider = SlowCountingProvider(SimulatorProvider(session_factory, clock), delay_seconds)
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
    return Harness(deps, provider, session_factory, credentials.merchant_id)


def _request() -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": HIGH_USD,
            "currency": "USD",
            "method": "card",
            "card": {"number": DECLINED_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _charge(h: Harness, key: str) -> ChargeOutcome:
    return charge_card(h.deps, h.merchant_id, key, _request())


def _charge_in_parallel(h: Harness) -> list[ChargeOutcome]:
    """Releases `BURST_SIZE` charges (distinct idempotency keys) at the same instant."""
    barrier = threading.Barrier(BURST_SIZE)

    def worker(index: int) -> ChargeOutcome:
        barrier.wait(timeout=30)
        return _charge(h, f"burst-{index}")

    with ThreadPoolExecutor(max_workers=BURST_SIZE) as pool:
        futures = [pool.submit(worker, index) for index in range(BURST_SIZE)]
        return [future.result(timeout=60) for future in futures]


def _charge_in_sequence(h: Harness) -> list[ChargeOutcome]:
    return [_charge(h, f"burst-{index}") for index in range(BURST_SIZE)]


def _reasons(value: object) -> tuple[str, ...]:
    assert isinstance(value, list)
    return tuple(str(reason) for reason in value)


def _assessments(
    session_factory: sessionmaker[Session],
) -> Counter[tuple[str, tuple[str, ...], bool, bool, str | None]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT risk_score, risk_reasons, risk_flagged, risk_blocked, decline_code "
                "FROM gateway.payments WHERE amount_minor = :amount"
            ),
            {"amount": HIGH_USD},
        ).mappings()
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


def _attempt_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.payment_attempts")).scalar_one())


EXPECTED_ASSESSMENTS = {
    ("0.40", ("high_amount",), False, False, "card_declined"): 2,
    ("0.70", ("high_amount", "recent_declines"), True, False, "card_declined"): 1,
    (
        "1.00",
        ("high_amount", "card_velocity", "recent_declines"),
        True,
        True,
        "risk_blocked",
    ): EXPECTED_BLOCKED,
}


@pytest.mark.parametrize("round_number", range(BURST_ROUNDS))
def test_parallel_high_amount_burst_on_a_declining_card_reaches_the_provider_three_times_and_blocks_the_rest(
    session_factory: sessionmaker[Session], clock: FixedClock, round_number: int
) -> None:
    # Arrange
    h = _harness(session_factory, clock, PROVIDER_DELAY_SECONDS)

    # Act
    outcomes = _charge_in_parallel(h)

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * BURST_SIZE
    assert Counter((outcome.body["state"], outcome.body["decline_code"]) for outcome in outcomes) == {
        ("declined", "card_declined"): EXPECTED_PROVIDER_CALLS,
        ("declined", "risk_blocked"): EXPECTED_BLOCKED,
    }
    assert h.provider.calls == EXPECTED_PROVIDER_CALLS
    assert _assessments(session_factory) == EXPECTED_ASSESSMENTS
    assert _attempt_count(session_factory) == EXPECTED_PROVIDER_CALLS


def test_sequential_high_amount_burst_on_a_new_declining_card_reaches_the_provider_three_times(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: the baseline the parallel burst must match
    h = _harness(session_factory, clock, 0.0)

    # Act
    outcomes = _charge_in_sequence(h)

    # Assert
    assert [outcome.status_code for outcome in outcomes] == [201] * BURST_SIZE
    assert Counter((outcome.body["state"], outcome.body["decline_code"]) for outcome in outcomes) == {
        ("declined", "card_declined"): EXPECTED_PROVIDER_CALLS,
        ("declined", "risk_blocked"): EXPECTED_BLOCKED,
    }
    assert h.provider.calls == EXPECTED_PROVIDER_CALLS
    assert _assessments(session_factory) == EXPECTED_ASSESSMENTS
