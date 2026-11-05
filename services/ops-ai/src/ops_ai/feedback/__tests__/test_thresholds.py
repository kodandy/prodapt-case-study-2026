"""Tests for ops_ai.feedback.thresholds (docs/specs/r2-agents.md section 1): the per-merchant fraud rule
versions, the threshold ladder, is_flagged and revert_rule."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.clock import Clock
from ops_ai.feedback.thresholds import (
    BASELINE,
    MAX_STEPS,
    RuleVersion,
    active_rule_version,
    is_flagged,
    revert_rule,
    rule_history,
    thresholds_for_steps,
)
from ops_ai.fraud_watch.analysis import CardTestingEpisode
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW


class _MutableClock:
    """Callable fixed clock whose instant can be moved forward."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
EPISODE_START = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)

INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES (:merchant_id, :version, "
    ":steps, :min_micro_payments, :min_micro_share, :min_distinct_cards, :reason, :feedback_id, "
    ":created_at)"
)


def _insert_version(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    version: int,
    steps: int,
    *,
    reason: str = "feedback:false_positive",
    feedback_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> datetime:
    min_micro_payments, min_micro_share, min_distinct_cards = thresholds_for_steps(steps)
    stored_at = created_at or TEST_NOW - timedelta(hours=10 - version)
    with session_factory.begin() as session:
        session.execute(
            INSERT_VERSION,
            {
                "merchant_id": merchant_id,
                "version": version,
                "steps": steps,
                "min_micro_payments": min_micro_payments,
                "min_micro_share": min_micro_share,
                "min_distinct_cards": min_distinct_cards,
                "reason": reason,
                "feedback_id": feedback_id,
                "created_at": stored_at,
            },
        )
    return stored_at


def _episode(
    micro_payments: int = 12, micro_share: float = 1.0, distinct_cards: int | None = 4
) -> CardTestingEpisode:
    return CardTestingEpisode(
        first_payment_id=uuid.UUID(int=1),
        start=EPISODE_START,
        end=EPISODE_START + timedelta(seconds=110),
        payment_ids=tuple(uuid.UUID(int=index + 1) for index in range(micro_payments)),
        micro_payments=micro_payments,
        card_payments=micro_payments,
        micro_share=micro_share,
        distinct_cards=distinct_cards,
        declined=0,
        decline_ratio=0.0,
        card_bins=("400000",),
        currencies=("USD",),
        flagged=False,
        reasons=(),
    )


def _rule_for_steps(steps: int) -> RuleVersion:
    min_micro_payments, min_micro_share, min_distinct_cards = thresholds_for_steps(steps)
    return RuleVersion(
        version=steps,
        steps=steps,
        min_micro_payments=min_micro_payments,
        min_micro_share=min_micro_share,
        min_distinct_cards=min_distinct_cards,
        reason="feedback:false_positive",
        feedback_id=None,
        created_at=TEST_NOW,
    )


def _version_count(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> int:
    with session_factory() as session:
        return int(
            session.execute(
                text("SELECT count(*) FROM ops_ai.fraud_rule_versions WHERE merchant_id = :m"),
                {"m": merchant_id},
            ).scalar_one()
        )


# --- thresholds_for_steps and BASELINE ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (0, (10, 0.8, 3)),
        (1, (12, 0.83, 4)),
        (2, (14, 0.86, 5)),
        (3, (16, 0.89, 6)),
        (4, (18, 0.92, 7)),
        (5, (20, 0.95, 8)),
    ],
)
def test_thresholds_for_steps_returns_the_ladder_value_for_each_step(
    steps: int, expected: tuple[int, float, int]
) -> None:
    # Arrange / Act
    result = thresholds_for_steps(steps)

    # Assert
    assert result == expected


@pytest.mark.parametrize(
    ("steps", "expected"),
    [(-1, (10, 0.8, 3)), (-100, (10, 0.8, 3)), (6, (20, 0.95, 8)), (9, (20, 0.95, 8))],
)
def test_thresholds_for_steps_clamps_steps_outside_zero_to_max_steps(
    steps: int, expected: tuple[int, float, int]
) -> None:
    # Arrange / Act
    result = thresholds_for_steps(steps)

    # Assert
    assert result == expected


def test_max_steps_is_five() -> None:
    # Arrange / Act
    value = MAX_STEPS

    # Assert
    assert value == 5


def test_no_step_is_looser_than_the_baseline() -> None:
    # Arrange
    baseline = thresholds_for_steps(0)

    # Act
    ladder = [thresholds_for_steps(steps) for steps in range(MAX_STEPS + 1)]

    # Assert
    assert all(
        payments >= baseline[0] and share >= baseline[1] and cards >= baseline[2]
        for payments, share, cards in ladder
    )


def test_baseline_is_version_zero_with_the_shipped_thresholds_and_no_row() -> None:
    # Arrange / Act
    baseline = BASELINE

    # Assert
    assert baseline == RuleVersion(
        version=0,
        steps=0,
        min_micro_payments=10,
        min_micro_share=0.8,
        min_distinct_cards=3,
        reason="baseline",
        feedback_id=None,
        created_at=None,
    )


def test_baseline_matches_the_zero_step_threshold_ladder() -> None:
    # Arrange / Act
    ladder = thresholds_for_steps(0)

    # Assert
    assert ladder == (BASELINE.min_micro_payments, BASELINE.min_micro_share, BASELINE.min_distinct_cards)


def test_a_rule_version_is_immutable() -> None:
    # Arrange
    rule = BASELINE

    # Act / Assert
    with pytest.raises(dataclasses.FrozenInstanceError):
        rule.steps = 3  # type: ignore[misc]


# --- is_flagged -----------------------------------------------------------------------------------------


def test_is_flagged_accepts_an_episode_exactly_on_every_baseline_boundary() -> None:
    # Arrange
    episode = _episode(micro_payments=10, micro_share=0.8, distinct_cards=3)

    # Act
    flagged = is_flagged(episode, BASELINE)

    # Assert
    assert flagged is True


def test_is_flagged_rejects_one_micro_payment_below_the_baseline() -> None:
    # Arrange
    episode = _episode(micro_payments=9, micro_share=1.0, distinct_cards=4)

    # Act
    flagged = is_flagged(episode, BASELINE)

    # Assert
    assert flagged is False


def test_is_flagged_rejects_a_micro_share_just_below_the_baseline() -> None:
    # Arrange
    episode = _episode(micro_payments=12, micro_share=0.79, distinct_cards=4)

    # Act
    flagged = is_flagged(episode, BASELINE)

    # Assert
    assert flagged is False


def test_is_flagged_rejects_one_distinct_card_below_the_baseline() -> None:
    # Arrange
    episode = _episode(micro_payments=12, micro_share=1.0, distinct_cards=2)

    # Act
    flagged = is_flagged(episode, BASELINE)

    # Assert
    assert flagged is False


def test_is_flagged_ignores_the_card_count_when_it_is_unknown() -> None:
    # Arrange
    episode = _episode(micro_payments=12, micro_share=1.0, distinct_cards=None)

    # Act
    flagged = is_flagged(episode, BASELINE)

    # Assert
    assert flagged is True


def test_is_flagged_with_an_unknown_card_count_still_needs_the_other_two_conditions() -> None:
    # Arrange
    few_payments = _episode(micro_payments=9, micro_share=1.0, distinct_cards=None)
    low_share = _episode(micro_payments=12, micro_share=0.79, distinct_cards=None)

    # Act
    results = (is_flagged(few_payments, BASELINE), is_flagged(low_share, BASELINE))

    # Assert
    assert results == (False, False)


@pytest.mark.parametrize(
    ("micro_payments", "micro_share", "distinct_cards", "expected"),
    [
        (20, 0.95, 8, True),
        (19, 0.95, 8, False),
        (20, 0.94, 8, False),
        (20, 0.95, 7, False),
        (20, 0.95, None, True),
        (25, 1.0, 12, True),
    ],
)
def test_is_flagged_applies_the_tightened_thresholds_of_the_maximum_step(
    micro_payments: int, micro_share: float, distinct_cards: int | None, expected: bool
) -> None:
    # Arrange
    rule = _rule_for_steps(MAX_STEPS)
    episode = _episode(micro_payments, micro_share, distinct_cards)

    # Act
    flagged = is_flagged(episode, rule)

    # Assert
    assert flagged is expected


def test_the_same_episode_is_flagged_at_the_baseline_and_not_after_a_false_positive_step() -> None:
    # Arrange
    episode = _episode(micro_payments=11, micro_share=0.9, distinct_cards=3)

    # Act
    results = (is_flagged(episode, BASELINE), is_flagged(episode, _rule_for_steps(1)))

    # Assert
    assert results == (True, False)


def test_is_flagged_ignores_the_flagged_attribute_of_the_episode() -> None:
    # Arrange
    episode = dataclasses.replace(_episode(micro_payments=5, micro_share=1.0, distinct_cards=5), flagged=True)

    # Act
    flagged = is_flagged(episode, BASELINE)

    # Assert
    assert flagged is False


# --- active_rule_version and rule_history ---------------------------------------------------------------


def test_active_rule_version_is_the_baseline_for_a_merchant_without_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    with session_factory() as session:
        active = active_rule_version(session, SCOPE)

    # Assert
    assert active == BASELINE


def test_active_rule_version_maps_the_stored_row_to_a_rule_version(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    feedback_id = uuid.uuid4()
    stored_at = _insert_version(session_factory, MERCHANT, 1, 1, feedback_id=feedback_id)

    # Act
    with session_factory() as session:
        active = active_rule_version(session, SCOPE)

    # Assert
    assert active == RuleVersion(
        version=1,
        steps=1,
        min_micro_payments=12,
        min_micro_share=0.83,
        min_distinct_cards=4,
        reason="feedback:false_positive",
        feedback_id=feedback_id,
        created_at=stored_at,
    )


def test_active_rule_version_is_the_newest_row_by_version_number(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, MERCHANT, 3, 3)
    _insert_version(session_factory, MERCHANT, 2, 2)

    # Act
    with session_factory() as session:
        active = active_rule_version(session, SCOPE)

    # Assert
    assert (active.version, active.steps) == (3, 3)


def test_active_rule_version_ignores_the_rows_of_another_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory, OTHER_MERCHANT, 1, 4)
    _insert_version(session_factory, OTHER_MERCHANT, 2, 5)

    # Act
    with session_factory() as session:
        own = active_rule_version(session, SCOPE)
        others = active_rule_version(session, OTHER_SCOPE)

    # Assert
    assert own == BASELINE
    assert (others.version, others.steps) == (2, 5)


def test_rule_history_is_empty_without_rows_and_never_lists_the_baseline(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    with session_factory() as session:
        history = rule_history(session, SCOPE)

    # Assert
    assert history == []


def test_rule_history_lists_the_merchants_rows_newest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, MERCHANT, 2, 2)
    _insert_version(session_factory, MERCHANT, 3, 1, reason="revert:v1")

    # Act
    with session_factory() as session:
        history = rule_history(session, SCOPE)

    # Assert
    assert [(rule.version, rule.steps, rule.reason) for rule in history] == [
        (3, 1, "revert:v1"),
        (2, 2, "feedback:false_positive"),
        (1, 1, "feedback:false_positive"),
    ]


def test_rule_history_excludes_the_rows_of_another_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, OTHER_MERCHANT, 1, 2)
    _insert_version(session_factory, OTHER_MERCHANT, 2, 3)

    # Act
    with session_factory() as session:
        history = rule_history(session, SCOPE)

    # Assert
    assert [(rule.version, rule.steps) for rule in history] == [(1, 1)]


# --- revert_rule ----------------------------------------------------------------------------------------


def test_revert_to_an_earlier_version_inserts_a_new_row_with_that_versions_values(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, MERCHANT, 2, 3)

    # Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 1)

    # Assert
    assert reverted == RuleVersion(
        version=3,
        steps=1,
        min_micro_payments=12,
        min_micro_share=0.83,
        min_distinct_cards=4,
        reason="revert:v1",
        feedback_id=None,
        created_at=TEST_NOW,
    )
    with session_factory() as session:
        assert active_rule_version(session, SCOPE) == reverted
        assert [rule.version for rule in rule_history(session, SCOPE)] == [3, 2, 1]


def test_revert_keeps_the_history_rows_it_reverted_over(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, MERCHANT, 2, 3)

    # Act
    with session_factory.begin() as session:
        revert_rule(session, SCOPE, clock, 1)

    # Assert
    with session_factory() as session:
        history = rule_history(session, SCOPE)
    assert [(rule.version, rule.steps) for rule in history] == [(3, 1), (2, 3), (1, 1)]


def test_revert_to_version_zero_restores_the_baseline_values_in_a_new_row(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, MERCHANT, 2, 2)

    # Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 0)

    # Assert
    assert reverted == RuleVersion(
        version=3,
        steps=0,
        min_micro_payments=10,
        min_micro_share=0.8,
        min_distinct_cards=3,
        reason="revert:v0",
        feedback_id=None,
        created_at=TEST_NOW,
    )
    with session_factory() as session:
        assert active_rule_version(session, SCOPE) == reverted


def test_revert_to_version_zero_without_any_row_creates_version_one(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange / Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 0)

    # Assert
    assert reverted is not None
    assert (reverted.version, reverted.steps, reverted.reason) == (1, 0, "revert:v0")
    assert _version_count(session_factory, MERCHANT) == 1


def test_revert_to_the_currently_active_version_still_inserts_a_new_row(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 2)

    # Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 1)

    # Assert
    assert reverted is not None
    assert (reverted.version, reverted.steps, reverted.reason) == (2, 2, "revert:v1")
    assert _version_count(session_factory, MERCHANT) == 2


def test_revert_to_an_unknown_version_returns_none_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)

    # Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 7)

    # Assert
    assert reverted is None
    assert _version_count(session_factory, MERCHANT) == 1


def test_revert_to_a_positive_version_without_any_row_returns_none(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange / Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 1)

    # Assert
    assert reverted is None
    assert _version_count(session_factory, MERCHANT) == 0


def test_revert_cannot_target_a_version_of_another_merchant(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, OTHER_MERCHANT, 1, 4)

    # Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 1)

    # Assert
    assert reverted is None
    assert _version_count(session_factory, MERCHANT) == 0
    assert _version_count(session_factory, OTHER_MERCHANT) == 1


def test_revert_leaves_the_other_merchants_rules_untouched(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, OTHER_MERCHANT, 1, 4)

    # Act
    with session_factory.begin() as session:
        revert_rule(session, SCOPE, clock, 0)

    # Assert
    with session_factory() as session:
        others = active_rule_version(session, OTHER_SCOPE)
    assert (others.version, others.steps) == (1, 4)
    assert _version_count(session_factory, OTHER_MERCHANT) == 1


def test_revert_does_not_commit(session_factory: sessionmaker[Session], clock: Clock) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 1)

    # Act
    with session_factory() as session:
        reverted = revert_rule(session, SCOPE, clock, 0)
        session.rollback()

    # Assert
    assert reverted is not None
    assert reverted.version == 2
    assert _version_count(session_factory, MERCHANT) == 1


def test_revert_stamps_the_row_with_the_injected_clock(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    clock = _MutableClock(TEST_NOW)
    _insert_version(session_factory, MERCHANT, 1, 1)
    clock.advance(timedelta(minutes=5))

    # Act
    with session_factory.begin() as session:
        reverted = revert_rule(session, SCOPE, clock, 0)

    # Assert
    assert reverted is not None
    assert reverted.created_at == TEST_NOW + timedelta(minutes=5)
