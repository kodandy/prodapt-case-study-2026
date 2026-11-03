"""RED tests for B7.1, written from docs/specs/gateway-basic.md (gateway/payments/risk.py).

Prior payments are seeded straight through the session (Payment + CardToken rows) so each rule is driven by
the exact `created_at` / `state` / fingerprint the test wants. The counts read the payment's own
`card_fingerprint` and `risk_blocked` columns (Decision 5, audit LOW-2), set by SQL after the insert.
"""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.models import Payment
from gateway.payments.risk import (
    BLOCK_THRESHOLD,
    DECLINE_LIMIT,
    DECLINE_WINDOW,
    FLAG_THRESHOLD,
    HIGH_AMOUNT_MINOR,
    VELOCITY_LIMIT,
    VELOCITY_WINDOW,
    RiskAssessment,
    assess_charge_risk,
)
from gateway.payments.tokenization.models import CardToken
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

FINGERPRINT = "a" * 64
OTHER_FINGERPRINT = "b" * 64
SMALL_AMOUNT = 1500
ONE_SECOND = timedelta(seconds=1)


def _seed_merchant(session: Session, now: datetime, name: str = "Acme") -> uuid.UUID:
    merchant_id = uuid.uuid4()
    session.add(Merchant(id=merchant_id, name=name, created_at=now))
    session.flush()
    return merchant_id


def _seed_payment(
    session: Session,
    merchant_id: uuid.UUID,
    created_at: datetime,
    state: str = "succeeded",
    fingerprint: str = FINGERPRINT,
    risk_blocked: bool = False,
    token_fingerprint: str | None = None,
    store_fingerprint: bool = True,
) -> None:
    """`token_fingerprint` and `store_fingerprint` let a test separate the token's fingerprint from the
    payment's own (None stored = a payment from before the change)."""
    token = f"tok_{uuid.uuid4().hex}"
    payment_id = uuid.uuid4()
    session.add(
        CardToken(
            token=token,
            fingerprint=fingerprint if token_fingerprint is None else token_fingerprint,
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
        {"fp": fingerprint if store_fingerprint else None, "blocked": risk_blocked, "id": payment_id},
    )


def _seed_many(
    session: Session,
    merchant_id: uuid.UUID,
    created_at: datetime,
    count: int,
    state: str = "succeeded",
    fingerprint: str = FINGERPRINT,
) -> None:
    for _ in range(count):
        _seed_payment(session, merchant_id, created_at, state, fingerprint)


def _assess(
    session: Session,
    now: datetime,
    amount_minor: int = SMALL_AMOUNT,
    currency: str = "USD",
    fingerprint: str = FINGERPRINT,
) -> RiskAssessment:
    return assess_charge_risk(session, now, fingerprint, amount_minor, currency)


# --- constants -----------------------------------------------------------------------------------------


def test_risk_constants_match_the_spec() -> None:
    # Arrange / Act
    constants = (
        HIGH_AMOUNT_MINOR,
        VELOCITY_LIMIT,
        VELOCITY_WINDOW,
        DECLINE_LIMIT,
        DECLINE_WINDOW,
        FLAG_THRESHOLD,
        BLOCK_THRESHOLD,
    )

    # Assert
    assert constants == (
        {"USD": 500_000, "EUR": 500_000, "JPY": 500_000, "INR": 40_000_000},
        3,
        timedelta(minutes=10),
        2,
        timedelta(minutes=60),
        Decimal("0.50"),
        Decimal("0.90"),
    )


def test_risk_assessment_is_a_frozen_value_object() -> None:
    # Arrange
    first = RiskAssessment(score=Decimal("0.40"), reasons=("high_amount",), flagged=False, blocked=False)
    second = RiskAssessment(score=Decimal("0.40"), reasons=("high_amount",), flagged=False, blocked=False)

    # Act
    hashes = (hash(first), hash(second))

    # Assert
    assert first == second
    assert hashes[0] == hashes[1]


# --- no rule hit ---------------------------------------------------------------------------------------


def test_assess_with_no_history_and_a_small_amount_scores_zero_with_no_reasons(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)
    assert str(result.score) == "0.00"


def test_assess_does_not_write_any_payment_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), 3)
        before = session.scalar(select(func.count()).select_from(Payment))
        _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"])
        after = session.scalar(select(func.count()).select_from(Payment))

    # Assert
    assert (before, after) == (3, 3)


# --- high_amount ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("currency", "limit"),
    [("USD", 500_000), ("EUR", 500_000), ("JPY", 500_000), ("INR", 40_000_000)],
)
def test_assess_amount_exactly_at_the_currency_limit_hits_high_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, currency: str, limit: int
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        result = _assess(session, now, amount_minor=limit, currency=currency)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.40"), reasons=("high_amount",), flagged=False, blocked=False
    )
    assert str(result.score) == "0.40"


@pytest.mark.parametrize(
    ("currency", "limit"),
    [("USD", 500_000), ("EUR", 500_000), ("JPY", 500_000), ("INR", 40_000_000)],
)
def test_assess_amount_one_below_the_currency_limit_does_not_hit_high_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, currency: str, limit: int
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        result = _assess(session, now, amount_minor=limit - 1, currency=currency)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_uses_the_limit_of_the_charged_currency_not_a_global_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        inr_below = _assess(session, now, amount_minor=500_000, currency="INR")
        usd_above = _assess(session, now, amount_minor=40_000_000, currency="USD")

    # Assert
    assert inr_below.reasons == ()
    assert usd_above.reasons == ("high_amount",)


# --- card_velocity -------------------------------------------------------------------------------------


def test_assess_three_earlier_payments_with_the_fingerprint_in_the_window_hit_card_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.40"), reasons=("card_velocity",), flagged=False, blocked=False
    )


def test_assess_one_below_the_velocity_limit_does_not_hit_card_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT - 1)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_more_payments_than_the_velocity_limit_add_the_weight_only_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT + 4)
        result = _assess(session, now)

    # Assert
    assert result.score == Decimal("0.40")
    assert result.reasons == ("card_velocity",)


def test_assess_a_payment_exactly_at_the_window_start_counts_toward_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT - 1)
        _seed_payment(session, merchant_id, now - VELOCITY_WINDOW)
        result = _assess(session, now)

    # Assert
    assert result.reasons == ("card_velocity",)
    assert result.score == Decimal("0.40")


def test_assess_a_payment_just_outside_the_velocity_window_is_excluded(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT - 1)
        _seed_payment(session, merchant_id, now - VELOCITY_WINDOW - ONE_SECOND)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_velocity_ignores_payments_of_other_fingerprints(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), 2)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), 5, fingerprint=OTHER_FINGERPRINT)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_velocity_counts_payments_of_any_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchants = [_seed_merchant(session, now, name=f"Merchant {index}") for index in range(3)]
        for merchant_id in merchants:
            _seed_payment(session, merchant_id, now - timedelta(minutes=2))
        result = _assess(session, now)

    # Assert
    assert result.reasons == ("card_velocity",)
    assert result.score == Decimal("0.40")


def test_assess_velocity_counts_payments_in_every_state(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - timedelta(minutes=1), state="succeeded")
        _seed_payment(session, merchant_id, now - timedelta(minutes=2), state="failed")
        _seed_payment(session, merchant_id, now - timedelta(minutes=3), state="unknown")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.40"), reasons=("card_velocity",), flagged=False, blocked=False
    )


# --- recent_declines -----------------------------------------------------------------------------------


def test_assess_two_earlier_declines_with_the_fingerprint_in_the_window_hit_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=30), DECLINE_LIMIT, state="declined")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.30"), reasons=("recent_declines",), flagged=False, blocked=False
    )
    assert str(result.score) == "0.30"


def test_assess_one_below_the_decline_limit_does_not_hit_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=30), DECLINE_LIMIT - 1, state="declined")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_a_decline_exactly_at_the_window_start_counts(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - timedelta(minutes=30), state="declined")
        _seed_payment(session, merchant_id, now - DECLINE_WINDOW, state="declined")
        result = _assess(session, now)

    # Assert
    assert result.reasons == ("recent_declines",)
    assert result.score == Decimal("0.30")


def test_assess_a_decline_just_outside_the_decline_window_is_excluded(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - timedelta(minutes=30), state="declined")
        _seed_payment(session, merchant_id, now - DECLINE_WINDOW - ONE_SECOND, state="declined")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_only_declined_payments_count_for_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - timedelta(minutes=30), state="declined")
        for state in ("succeeded", "failed", "unknown", "created"):
            _seed_payment(session, merchant_id, now - timedelta(minutes=30), state=state)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_declines_of_other_fingerprints_are_ignored(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - timedelta(minutes=30), state="declined")
        _seed_many(
            session,
            merchant_id,
            now - timedelta(minutes=30),
            4,
            state="declined",
            fingerprint=OTHER_FINGERPRINT,
        )
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_declines_of_any_merchant_count(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        first = _seed_merchant(session, now, name="First")
        second = _seed_merchant(session, now, name="Second")
        _seed_payment(session, first, now - timedelta(minutes=30), state="declined")
        _seed_payment(session, second, now - timedelta(minutes=30), state="declined")
        result = _assess(session, now)

    # Assert
    assert result.reasons == ("recent_declines",)
    assert result.score == Decimal("0.30")


def test_assess_more_declines_than_the_limit_add_the_weight_only_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=30), DECLINE_LIMIT + 5, state="declined")
        result = _assess(session, now)

    # Assert
    assert result.score == Decimal("0.30")
    assert result.reasons == ("recent_declines",)


def test_assess_the_two_windows_are_independent(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: 11 minutes ago is outside the 10-minute velocity window, inside the 60-minute decline one
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=11), 3, state="declined")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.30"), reasons=("recent_declines",), flagged=False, blocked=False
    )


def test_assess_risk_blocked_declines_do_not_count_toward_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        for _ in range(DECLINE_LIMIT + 1):
            _seed_payment(
                session, merchant_id, now - timedelta(minutes=30), state="declined", risk_blocked=True
            )
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_a_risk_blocked_decline_does_not_complete_the_decline_limit_with_a_provider_decline(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_payment(session, merchant_id, now - timedelta(minutes=30), state="declined")
        _seed_payment(session, merchant_id, now - timedelta(minutes=30), state="declined", risk_blocked=True)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_risk_blocked_payments_still_count_toward_card_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        for _ in range(VELOCITY_LIMIT):
            _seed_payment(
                session, merchant_id, now - timedelta(minutes=1), state="declined", risk_blocked=True
            )
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.40"), reasons=("card_velocity",), flagged=False, blocked=False
    )


# --- the payment's own fingerprint (Decision 5) --------------------------------------------------------


def test_assess_counts_payments_by_their_own_fingerprint_not_by_the_token_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: the stored fingerprint matches, the token row's does not
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        for _ in range(VELOCITY_LIMIT):
            _seed_payment(
                session, merchant_id, now - timedelta(minutes=1), token_fingerprint=OTHER_FINGERPRINT
            )
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.40"), reasons=("card_velocity",), flagged=False, blocked=False
    )


def test_assess_does_not_count_a_payment_whose_own_fingerprint_differs_even_if_its_token_matches(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        for _ in range(VELOCITY_LIMIT):
            _seed_payment(
                session,
                merchant_id,
                now - timedelta(minutes=1),
                fingerprint=OTHER_FINGERPRINT,
                token_fingerprint=FINGERPRINT,
            )
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def test_assess_ignores_payments_from_before_the_change_that_have_no_stored_fingerprint(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        for _ in range(VELOCITY_LIMIT):
            _seed_payment(session, merchant_id, now - timedelta(minutes=1), store_fingerprint=False)
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


# --- combinations, cap, thresholds ---------------------------------------------------------------------


def test_assess_high_amount_and_velocity_score_eighty_hundredths_and_flag_without_blocking(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT)
        result = _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"])

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.80"), reasons=("high_amount", "card_velocity"), flagged=True, blocked=False
    )
    assert str(result.score) == "0.80"


def test_assess_high_amount_and_declines_score_seventy_hundredths_and_flag(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: declines 30 minutes old keep the velocity window empty
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=30), DECLINE_LIMIT, state="declined")
        result = _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"])

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.70"), reasons=("high_amount", "recent_declines"), flagged=True, blocked=False
    )


def test_assess_velocity_and_declines_score_seventy_hundredths_and_flag(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT, state="declined")
        result = _assess(session, now)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.70"), reasons=("card_velocity", "recent_declines"), flagged=True, blocked=False
    )


def test_assess_all_three_rules_cap_the_score_at_one_and_block(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT, state="declined")
        result = _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"])

    # Assert
    assert result == RiskAssessment(
        score=Decimal("1.00"),
        reasons=("high_amount", "card_velocity", "recent_declines"),
        flagged=True,
        blocked=True,
    )
    assert str(result.score) == "1.00"


def test_assess_reasons_follow_the_fixed_rule_order_regardless_of_data_insertion_order(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange: declines are seeded first, the amount is checked last; the order must still be rule order
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=5), 2, state="declined")
        _seed_payment(session, merchant_id, now - timedelta(minutes=2), state="succeeded")
        result = _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["EUR"], currency="EUR")

    # Assert
    assert result.reasons == ("high_amount", "card_velocity", "recent_declines")


def test_assess_no_single_rule_reaches_the_flag_threshold(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        high_only = _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"])
        _seed_many(session, merchant_id, now - timedelta(minutes=30), DECLINE_LIMIT, state="declined")
        declines_only = _assess(session, now)

    # Assert
    assert (high_only.score, high_only.flagged, high_only.blocked) == (Decimal("0.40"), False, False)
    assert (declines_only.score, declines_only.flagged, declines_only.blocked) == (
        Decimal("0.30"),
        False,
        False,
    )
    assert high_only.score < FLAG_THRESHOLD
    assert declines_only.score < FLAG_THRESHOLD


def test_assess_a_flagged_score_below_the_block_threshold_is_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT)
        result = _assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["JPY"], currency="JPY")

    # Assert
    assert FLAG_THRESHOLD <= result.score < BLOCK_THRESHOLD
    assert (result.flagged, result.blocked) == (True, False)


def test_assess_score_is_a_decimal_with_two_decimal_places_for_every_combination(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    now = clock()
    scores: list[Decimal] = []

    # Act
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        scores.append(_assess(session, now).score)
        scores.append(_assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"]).score)
        _seed_many(session, merchant_id, now - timedelta(minutes=1), VELOCITY_LIMIT, state="declined")
        scores.append(_assess(session, now).score)
        scores.append(_assess(session, now, amount_minor=HIGH_AMOUNT_MINOR["USD"]).score)

    # Assert
    assert [str(score) for score in scores] == ["0.00", "0.40", "0.70", "1.00"]
    assert all(isinstance(score, Decimal) for score in scores)
