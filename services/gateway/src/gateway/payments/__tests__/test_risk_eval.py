"""RED tests for B7.3, from docs/specs/disputes-notifications.md section 5 (gateway/payments/risk_eval.py).

The spec names `score_activity`, `labeled_groups`, `replay`, `measure` and `RiskEvalResult` but not the shape
of a group. These tests state the shape they rely on as the `GroupShape` / `ChargeShape` / `AssessmentShape`
protocols below, so a different shape is a one-place change:

- a group has `.charges`, a chronological sequence of charges on ONE card;
- a charge has `.offset_seconds`, `.amount_minor`, `.currency` and `.declined`;
- `score_activity` and `replay` yield objects with `.score`, `.reasons`, `.flagged` and `.blocked`
  (`replay(group)` gives one per charge, in charge order).

Everything else (which groups are legitimate, which are card-testing bursts) is read through `measure`, so
the tests do not depend on a label attribute.
"""

import subprocess
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

import pytest
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.models import Payment
from gateway.payments.risk import (
    DECLINE_LIMIT,
    DECLINE_WINDOW,
    HIGH_AMOUNT_MINOR,
    VELOCITY_LIMIT,
    VELOCITY_WINDOW,
    assess_charge_risk,
    card_activity,
)
from gateway.payments.risk_eval import RiskEvalResult, labeled_groups, measure, replay, score_activity
from gateway.payments.tokenization.models import CardToken
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SEED = 20261103
OTHER_SEED = 7
FINGERPRINT = "c" * 64
SMALL_AMOUNT = 1500
HIGH_USD = HIGH_AMOUNT_MINOR["USD"]
FREE_CHARGES_BEFORE_VELOCITY = VELOCITY_LIMIT
SUB_TWO_DOLLARS = 200


class ChargeShape(Protocol):
    @property
    def offset_seconds(self) -> float: ...

    @property
    def amount_minor(self) -> int: ...

    @property
    def currency(self) -> str: ...

    @property
    def declined(self) -> bool: ...


class GroupShape(Protocol):
    @property
    def charges(self) -> Sequence[ChargeShape]: ...


class AssessmentShape(Protocol):
    @property
    def score(self) -> Decimal: ...

    @property
    def reasons(self) -> tuple[str, ...]: ...

    @property
    def flagged(self) -> bool: ...

    @property
    def blocked(self) -> bool: ...


Outcome = tuple[Decimal, tuple[str, ...], bool, bool]


def _outcome(assessment: AssessmentShape) -> Outcome:
    return assessment.score, tuple(assessment.reasons), assessment.flagged, assessment.blocked


def _expected_outcomes(group: GroupShape) -> list[Outcome]:
    """The rule applied charge by charge with card_activity's windows over the EARLIER charges only."""
    charges = list(group.charges)
    outcomes: list[Outcome] = []
    for index, charge in enumerate(charges):
        now = charge.offset_seconds
        earlier = charges[:index]
        recent = sum(1 for c in earlier if c.offset_seconds >= now - VELOCITY_WINDOW.total_seconds())
        declines = sum(
            1 for c in earlier if c.declined and c.offset_seconds >= now - DECLINE_WINDOW.total_seconds()
        )
        outcomes.append(_outcome(score_activity(charge.amount_minor, charge.currency, recent, declines)))
    return outcomes


# --- score_activity: the pure rule ----------------------------------------------------------------------


def test_no_rule_hit_scores_zero_with_no_reasons() -> None:
    # Arrange / Act
    assessment = score_activity(SMALL_AMOUNT, "USD", 0, 0)

    # Assert
    assert _outcome(assessment) == (Decimal("0.00"), (), False, False)


@pytest.mark.parametrize(
    ("amount_minor", "recent", "declines", "score", "reasons", "flagged", "blocked"),
    [
        (HIGH_USD, 0, 0, "0.40", ("high_amount",), False, False),
        (SMALL_AMOUNT, 3, 0, "0.40", ("card_velocity",), False, False),
        (SMALL_AMOUNT, 0, 2, "0.30", ("recent_declines",), False, False),
        (SMALL_AMOUNT, 3, 2, "0.70", ("card_velocity", "recent_declines"), True, False),
        (HIGH_USD, 3, 0, "0.80", ("high_amount", "card_velocity"), True, False),
        (HIGH_USD, 0, 2, "0.70", ("high_amount", "recent_declines"), True, False),
        (HIGH_USD, 3, 2, "1.00", ("high_amount", "card_velocity", "recent_declines"), True, True),
    ],
    ids=["amount", "velocity", "declines", "velocity+declines", "amount+velocity", "amount+declines", "all"],
)
def test_each_rule_combination_scores_and_flags_as_the_charge_rules_do(
    amount_minor: int,
    recent: int,
    declines: int,
    score: str,
    reasons: tuple[str, ...],
    flagged: bool,
    blocked: bool,
) -> None:
    # Arrange / Act
    assessment = score_activity(amount_minor, "USD", recent, declines)

    # Assert
    assert _outcome(assessment) == (Decimal(score), reasons, flagged, blocked)


@pytest.mark.parametrize(
    ("recent", "declines", "expected_reasons"),
    [
        (VELOCITY_LIMIT - 1, DECLINE_LIMIT - 1, ()),
        (VELOCITY_LIMIT, DECLINE_LIMIT - 1, ("card_velocity",)),
        (VELOCITY_LIMIT - 1, DECLINE_LIMIT, ("recent_declines",)),
        (VELOCITY_LIMIT + 5, DECLINE_LIMIT + 5, ("card_velocity", "recent_declines")),
    ],
    ids=["below-both", "velocity-at-limit", "declines-at-limit", "far-above-both"],
)
def test_the_velocity_and_decline_rules_start_exactly_at_their_limits(
    recent: int, declines: int, expected_reasons: tuple[str, ...]
) -> None:
    # Arrange / Act
    assessment = score_activity(SMALL_AMOUNT, "USD", recent, declines)

    # Assert
    assert tuple(assessment.reasons) == expected_reasons


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "INR"])
def test_the_amount_rule_starts_exactly_at_the_currency_threshold(currency: str) -> None:
    # Arrange
    threshold = HIGH_AMOUNT_MINOR[currency]

    # Act
    below = score_activity(threshold - 1, currency, 0, 0)
    at = score_activity(threshold, currency, 0, 0)

    # Assert
    assert tuple(below.reasons) == ()
    assert tuple(at.reasons) == ("high_amount",)


def test_the_score_is_capped_at_one() -> None:
    # Arrange / Act
    assessment = score_activity(HIGH_USD, "USD", 50, 50)

    # Assert
    assert assessment.score == Decimal("1.00")
    assert assessment.blocked is True


@pytest.mark.parametrize(
    ("amount_minor", "recent", "declines"),
    [(SMALL_AMOUNT, 0, 0), (HIGH_USD, 0, 0), (SMALL_AMOUNT, 3, 2), (HIGH_USD, 3, 0), (HIGH_USD, 4, 4)],
)
def test_score_activity_agrees_with_assess_charge_risk_for_the_same_activity(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    amount_minor: int,
    recent: int,
    declines: int,
) -> None:
    # Arrange
    def lookup(session: Session, now: datetime, card_fingerprint: str) -> tuple[int, int]:
        return recent, declines

    # Act
    with session_factory() as session:
        assessed = assess_charge_risk(
            session, clock(), FINGERPRINT, amount_minor, "USD", velocity_lookup=lookup
        )

    # Assert
    assert _outcome(assessed) == _outcome(score_activity(amount_minor, "USD", recent, declines))


# --- labeled_groups --------------------------------------------------------------------------------------


def test_labeled_groups_is_deterministic_for_one_seed() -> None:
    # Arrange / Act
    first = labeled_groups(SEED)
    second = labeled_groups(SEED)

    # Assert
    assert len(first) > 0
    assert first == second


def test_labeled_groups_holds_legitimate_groups_and_card_testing_bursts() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    result = measure(groups)

    # Assert
    assert result.legit_groups > 0
    assert result.burst_groups > 0
    assert result.legit_groups + result.burst_groups == len(groups)


@pytest.mark.parametrize("seed", [SEED, OTHER_SEED])
def test_every_group_is_a_chronological_run_of_micro_charges_that_never_trips_the_amount_rule(
    seed: int,
) -> None:
    # Arrange
    groups = labeled_groups(seed)

    # Act
    offsets = [[c.offset_seconds for c in g.charges] for g in groups]
    over_threshold = [
        c.amount_minor for g in groups for c in g.charges if c.amount_minor >= HIGH_AMOUNT_MINOR[c.currency]
    ]

    # Assert
    assert all(len(group_offsets) >= 1 for group_offsets in offsets)
    assert all(group_offsets == sorted(group_offsets) for group_offsets in offsets)
    assert over_threshold == []


def test_a_burst_is_many_sub_two_dollar_charges_that_are_mostly_declined() -> None:
    # Arrange
    bursts = [g for g in labeled_groups(SEED) if measure([g]).burst_groups == 1]

    # Act
    sizes = [len(g.charges) for g in bursts]
    declined_shares = [sum(c.declined for c in g.charges) / len(g.charges) for g in bursts]
    currencies = {c.currency for g in bursts for c in g.charges}
    amounts = [c.amount_minor for g in bursts for c in g.charges]

    # Assert
    assert min(sizes) >= VELOCITY_LIMIT + 1
    assert min(declined_shares) > 0.5
    assert currencies == {"USD"}
    assert max(amounts) < SUB_TWO_DOLLARS


# --- replay ----------------------------------------------------------------------------------------------


def test_replay_returns_one_assessment_per_charge_in_charge_order() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    lengths = [(len(replay(g)), len(g.charges)) for g in groups]

    # Assert
    assert all(assessed == charged for assessed, charged in lengths)


def test_replay_is_repeatable() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    first = [[_outcome(a) for a in replay(g)] for g in groups]
    second = [[_outcome(a) for a in replay(g)] for g in groups]

    # Assert
    assert first == second


def test_replay_never_flags_a_charge_that_has_fewer_than_the_velocity_limit_of_earlier_charges() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    early = [a for g in groups for a in replay(g)[:FREE_CHARGES_BEFORE_VELOCITY]]

    # Assert
    assert len(early) > 0
    assert [a for a in early if a.flagged or a.blocked] == []


def test_replay_applies_card_activitys_windows_to_the_earlier_charges_of_each_group() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    replayed = [[_outcome(a) for a in replay(g)] for g in groups]
    expected = [_expected_outcomes(g) for g in groups]

    # Assert
    assert replayed == expected


def _seed_card_history(
    session: Session, merchant_id: uuid.UUID, created_at: datetime, declined: bool
) -> None:
    token = f"tok_{uuid.uuid4().hex}"
    payment_id = uuid.uuid4()
    session.add(
        CardToken(
            token=token,
            fingerprint=FINGERPRINT,
            bin="424242",
            last4="4242",
            exp_month=12,
            exp_year=2030,
            created_at=created_at,
        )
    )
    session.flush()
    session.add(
        Payment(
            id=payment_id,
            merchant_id=merchant_id,
            amount_minor=SMALL_AMOUNT,
            currency="USD",
            method="card",
            state="declined" if declined else "succeeded",
            card_token=token,
            entity_version=1,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    session.flush()
    session.execute(
        text("UPDATE gateway.payments SET card_fingerprint = :fp, risk_blocked = false WHERE id = :id"),
        {"fp": FINGERPRINT, "id": payment_id},
    )


@pytest.mark.parametrize("burst", [False, True], ids=["legitimate-group", "card-testing-burst"])
def test_replay_matches_the_real_card_activity_over_payments_seeded_one_by_one(
    session_factory: sessionmaker[Session], clock: FixedClock, burst: bool
) -> None:
    # Arrange
    group = next(g for g in labeled_groups(SEED) if (measure([g]).burst_groups == 1) is burst)
    replayed = [_outcome(a) for a in replay(group)]
    base = clock()
    from_database: list[Outcome] = []

    # Act
    with session_factory() as session:
        merchant_id = uuid.uuid4()
        session.add(Merchant(id=merchant_id, name="Replay", created_at=base))
        session.flush()
        for charge in group.charges:
            now = base + timedelta(seconds=charge.offset_seconds)
            recent, declines = card_activity(session, now, FINGERPRINT)
            from_database.append(
                _outcome(score_activity(charge.amount_minor, charge.currency, recent, declines))
            )
            _seed_card_history(session, merchant_id, now, charge.declined)

    # Assert
    assert from_database == replayed


# --- measure ---------------------------------------------------------------------------------------------


def test_risk_eval_result_exposes_the_specified_fields() -> None:
    # Arrange / Act
    result = RiskEvalResult(
        legit_groups=10,
        burst_groups=4,
        false_positives=1,
        detected=3,
        false_positive_rate=0.1,
        detection_rate=0.75,
    )

    # Assert
    assert (result.legit_groups, result.burst_groups) == (10, 4)
    assert (result.false_positives, result.detected) == (1, 3)
    assert (result.false_positive_rate, result.detection_rate) == (0.1, 0.75)
    assert result == RiskEvalResult(10, 4, 1, 3, 0.1, 0.75)


def test_measure_rates_are_the_counts_over_their_group_totals() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    result = measure(groups)

    # Assert
    assert 0 <= result.false_positives <= result.legit_groups
    assert 0 <= result.detected <= result.burst_groups
    assert result.false_positive_rate == pytest.approx(result.false_positives / result.legit_groups)
    assert result.detection_rate == pytest.approx(result.detected / result.burst_groups)


def test_every_card_testing_burst_in_the_labeled_set_is_detected() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    result = measure(groups)

    # Assert
    assert result.detected == result.burst_groups
    assert result.detection_rate == pytest.approx(1.0)


def test_a_group_counts_as_positive_when_any_charge_is_flagged_or_blocked() -> None:
    # Arrange
    groups = labeled_groups(SEED)

    # Act
    mismatches = []
    for group in groups:
        positive = any(a.flagged or a.blocked for a in replay(group))
        single = measure([group])
        counted = single.false_positives + single.detected
        if counted != int(positive):
            mismatches.append((single, positive))

    # Assert
    assert mismatches == []


def test_measure_counts_scale_with_repeated_groups_and_the_rates_stay_the_same() -> None:
    # Arrange
    groups = labeled_groups(SEED)
    once = measure(groups)

    # Act
    twice = measure([*groups, *groups])

    # Assert
    assert (twice.legit_groups, twice.burst_groups) == (2 * once.legit_groups, 2 * once.burst_groups)
    assert (twice.false_positives, twice.detected) == (2 * once.false_positives, 2 * once.detected)
    assert twice.false_positive_rate == pytest.approx(once.false_positive_rate)
    assert twice.detection_rate == pytest.approx(once.detection_rate)


# --- python -m gateway.payments.risk_eval -----------------------------------------------------------------


def test_running_the_module_prints_the_measured_result_and_exits_zero() -> None:
    # Arrange / Act
    completed = subprocess.run(
        [sys.executable, "-m", "gateway.payments.risk_eval"],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    # Assert
    assert completed.returncode == 0
    for name in ("legit_groups", "burst_groups", "false_positives", "detected"):
        assert name in completed.stdout
    assert "false_positive_rate" in completed.stdout
    assert "detection_rate" in completed.stdout
