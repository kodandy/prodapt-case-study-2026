"""Pure tests of incident reranking, written from docs/specs/r2-agents.md section 2 (R2-4).

score = 0.5 * relevance + 0.2 * recency + 0.3 * effectiveness; relevance is normalised by the batch maximum,
recency halves every 30 days, effectiveness is a Laplace-smoothed helpful ratio halved on recurrence.
"""

import math
from datetime import datetime, timedelta, timezone

import pytest
from ops_ai.search.rerank import (
    RECENCY_HALF_LIFE_DAYS,
    REOPEN_PENALTY,
    WEIGHT_EFFECTIVENESS,
    WEIGHT_RECENCY,
    WEIGHT_RELEVANCE,
    IncidentCandidate,
    RankedIncident,
    effectiveness,
    recency,
    rerank_incidents,
)

from conftest import TEST_NOW

TOLERANCE = 1e-9


def _candidate(incident_id: str = "INC-1", **overrides: object) -> IncidentCandidate:
    fields: dict[str, object] = {
        "incident_id": incident_id,
        "title": f"title of {incident_id}",
        "root_cause": f"root cause of {incident_id}",
        "fix_applied": f"fix of {incident_id}",
        "started_at": TEST_NOW - timedelta(days=2),
        "resolved_at": TEST_NOW - timedelta(days=1),
        "recurrence_within_7d": False,
        "relevance": 0.03,
        "helpful": 0,
        "not_helpful": 0,
    }
    fields.update(overrides)
    return IncidentCandidate(**fields)  # type: ignore[arg-type]


def _ids(ranked: list[RankedIncident]) -> list[str]:
    return [item.incident_id for item in ranked]


def test_the_constants_have_the_specified_values() -> None:
    # Arrange / Act
    constants = (
        RECENCY_HALF_LIFE_DAYS,
        WEIGHT_RELEVANCE,
        WEIGHT_RECENCY,
        WEIGHT_EFFECTIVENESS,
        REOPEN_PENALTY,
    )

    # Assert
    assert constants == (30.0, 0.5, 0.2, 0.3, 0.5)


# --- effectiveness -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("resolved", "recurred", "helpful", "not_helpful", "expected"),
    [
        (True, False, 0, 0, 0.5),
        (True, False, 2, 0, 0.75),
        (True, False, 0, 2, 0.25),
        (True, False, 1, 1, 0.5),
        (True, False, 5, 0, 6 / 7),
        (True, True, 0, 0, 0.25),
        (True, True, 2, 0, 0.375),
        (False, False, 0, 0, 0.0),
        (False, False, 9, 0, 0.0),
        (False, True, 3, 1, 0.0),
    ],
    ids=[
        "resolved_no_votes",
        "two_helpful",
        "two_not_helpful",
        "one_each",
        "five_helpful",
        "recurred_no_votes",
        "recurred_two_helpful",
        "unresolved",
        "unresolved_with_helpful_votes",
        "unresolved_and_recurred",
    ],
)
def test_effectiveness_is_a_smoothed_helpful_ratio_zero_when_unresolved_and_halved_when_recurred(
    resolved: bool, recurred: bool, helpful: int, not_helpful: int, expected: float
) -> None:
    # Arrange / Act
    value = effectiveness(resolved, recurred, helpful, not_helpful)

    # Assert
    assert value == pytest.approx(expected, abs=TOLERANCE)


# --- recency -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(0), 1.0),
        (timedelta(days=30), 0.5),
        (timedelta(days=60), 0.25),
        (timedelta(days=90), 0.125),
        (timedelta(days=15), math.sqrt(0.5)),
        (timedelta(days=-10), 1.0),
        (timedelta(seconds=-1), 1.0),
    ],
    ids=["now", "one_half_life", "two_halves", "three_halves", "half_life_half", "future", "future_1s"],
)
def test_recency_halves_every_30_days_and_clamps_the_future_to_one(age: timedelta, expected: float) -> None:
    # Arrange
    at = TEST_NOW - age

    # Act
    value = recency(at, TEST_NOW)

    # Assert
    assert value == pytest.approx(expected, abs=TOLERANCE)


def test_recency_compares_instants_across_time_zones() -> None:
    # Arrange: 2026-10-04 13:00 at +01:00 is 12:00 UTC, exactly 30 days before TEST_NOW
    at = datetime(2026, 10, 4, 13, 0, tzinfo=timezone(timedelta(hours=1)))

    # Act
    value = recency(at, TEST_NOW)

    # Assert
    assert value == pytest.approx(0.5, abs=TOLERANCE)


# --- rerank_incidents --------------------------------------------------------------------------------------


def test_rerank_incidents_of_no_candidates_is_an_empty_list() -> None:
    # Arrange / Act
    ranked = rerank_incidents([], TEST_NOW)

    # Assert
    assert ranked == []


def test_a_recent_effective_incident_beats_an_older_equally_relevant_one() -> None:
    # Arrange: listed older first so that the input order cannot explain the result
    older = _candidate(
        "INC-A", started_at=TEST_NOW - timedelta(days=100), resolved_at=TEST_NOW - timedelta(days=90)
    )
    recent = _candidate(
        "INC-B", started_at=TEST_NOW - timedelta(days=3), resolved_at=TEST_NOW - timedelta(days=1)
    )

    # Act
    ranked = rerank_incidents([older, recent], TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert ranked[0].score > ranked[1].score


def test_a_recurred_incident_drops_below_a_non_recurred_one() -> None:
    # Arrange: the recurred one sorts first by id, so only the penalty can push it down
    recurred = _candidate("INC-A", recurrence_within_7d=True)
    stable = _candidate("INC-B", recurrence_within_7d=False)

    # Act
    ranked = rerank_incidents([recurred, stable], TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert (ranked[0].effectiveness, ranked[1].effectiveness) == pytest.approx((0.5, 0.25), abs=TOLERANCE)


def test_an_unresolved_incident_has_zero_effectiveness_and_ranks_below_a_resolved_one() -> None:
    # Arrange
    unresolved = _candidate("INC-A", resolved_at=None, helpful=4)
    resolved = _candidate("INC-B")

    # Act
    ranked = rerank_incidents([unresolved, resolved], TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert ranked[1].effectiveness == 0.0
    assert ranked[1].resolved_at is None


def test_an_unresolved_incident_takes_its_recency_from_started_at() -> None:
    # Arrange
    unresolved = _candidate("INC-A", started_at=TEST_NOW - timedelta(days=30), resolved_at=None)

    # Act
    (ranked,) = rerank_incidents([unresolved], TEST_NOW)

    # Assert
    assert ranked.recency == pytest.approx(0.5, abs=TOLERANCE)


def test_a_resolved_incident_takes_its_recency_from_resolved_at_not_started_at() -> None:
    # Arrange
    resolved = _candidate(
        "INC-A", started_at=TEST_NOW - timedelta(days=120), resolved_at=TEST_NOW - timedelta(days=60)
    )

    # Act
    (ranked,) = rerank_incidents([resolved], TEST_NOW)

    # Assert
    assert ranked.recency == pytest.approx(0.25, abs=TOLERANCE)


def test_helpful_feedback_lifts_one_incident_above_another() -> None:
    # Arrange: the helpful one sorts last by id, so only the feedback can lift it
    plain = _candidate("INC-A")
    liked = _candidate("INC-B", helpful=3)

    # Act
    ranked = rerank_incidents([plain, liked], TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert (ranked[0].effectiveness, ranked[1].effectiveness) == pytest.approx((0.8, 0.5), abs=TOLERANCE)


def test_not_helpful_feedback_pushes_one_incident_below_another() -> None:
    # Arrange
    disliked = _candidate("INC-A", not_helpful=3)
    plain = _candidate("INC-B")

    # Act
    ranked = rerank_incidents([disliked, plain], TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert ranked[1].effectiveness == pytest.approx(0.2, abs=TOLERANCE)


def test_identical_candidates_are_ordered_by_incident_id_ascending() -> None:
    # Arrange
    candidates = [_candidate("INC-C"), _candidate("INC-A"), _candidate("INC-B")]

    # Act
    ranked = rerank_incidents(candidates, TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-A", "INC-B", "INC-C"]
    assert len({item.score for item in ranked}) == 1


def test_a_lower_relevance_can_win_on_recency_and_effectiveness() -> None:
    # Arrange: A is the most relevant but unresolved and ancient; B is 80% as relevant but fresh and resolved
    ancient = _candidate("INC-A", relevance=1.0, started_at=TEST_NOW - timedelta(days=300), resolved_at=None)
    fresh = _candidate("INC-B", relevance=0.8, resolved_at=TEST_NOW)

    # Act
    ranked = rerank_incidents([ancient, fresh], TEST_NOW)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert ranked[0].score == pytest.approx(0.5 * 0.8 + 0.2 * 1.0 + 0.3 * 0.5, abs=TOLERANCE)
    assert ranked[1].score == pytest.approx(0.5 * 1.0 + 0.2 * 0.5**10 + 0.3 * 0.0, abs=TOLERANCE)


def test_relevance_is_normalised_by_the_maximum_of_the_batch() -> None:
    # Arrange
    best = _candidate("INC-A", relevance=0.04)
    half = _candidate("INC-B", relevance=0.02)
    quarter = _candidate("INC-C", relevance=0.01)

    # Act
    ranked = rerank_incidents([quarter, half, best], TEST_NOW)

    # Assert
    assert [(item.incident_id, item.relevance) for item in ranked] == pytest.approx(
        [("INC-A", 1.0), ("INC-B", 0.5), ("INC-C", 0.25)], abs=TOLERANCE
    )


def test_relevance_is_zero_for_every_candidate_when_the_batch_maximum_is_zero() -> None:
    # Arrange
    candidates = [_candidate("INC-A", relevance=0.0), _candidate("INC-B", relevance=0.0)]

    # Act
    ranked = rerank_incidents(candidates, TEST_NOW)

    # Assert
    assert [item.relevance for item in ranked] == [0.0, 0.0]
    expected = 0.2 * 0.5 ** (1 / 30) + 0.3 * 0.5
    assert [item.score for item in ranked] == pytest.approx([expected, expected], abs=TOLERANCE)


def test_a_single_candidate_with_positive_relevance_is_normalised_to_one() -> None:
    # Arrange
    only = _candidate("INC-A", relevance=0.0163)

    # Act
    (ranked,) = rerank_incidents([only], TEST_NOW)

    # Assert
    assert ranked.relevance == pytest.approx(1.0, abs=TOLERANCE)


def test_the_score_is_the_weighted_sum_of_relevance_recency_and_effectiveness() -> None:
    # Arrange: relevance 0.03 / max 0.06 = 0.5; resolved 30 days ago = recency 0.5; 2 helpful = 0.75
    scored = _candidate(
        "INC-A",
        relevance=0.03,
        started_at=TEST_NOW - timedelta(days=40),
        resolved_at=TEST_NOW - timedelta(days=30),
        helpful=2,
    )
    top = _candidate("INC-B", relevance=0.06, resolved_at=TEST_NOW)

    # Act
    ranked = {item.incident_id: item for item in rerank_incidents([scored, top], TEST_NOW)}

    # Assert
    assert ranked["INC-A"].relevance == pytest.approx(0.5, abs=TOLERANCE)
    assert ranked["INC-A"].recency == pytest.approx(0.5, abs=TOLERANCE)
    assert ranked["INC-A"].effectiveness == pytest.approx(0.75, abs=TOLERANCE)
    assert ranked["INC-A"].score == pytest.approx(0.5 * 0.5 + 0.2 * 0.5 + 0.3 * 0.75, abs=TOLERANCE)
    assert ranked["INC-A"].score == pytest.approx(0.575, abs=TOLERANCE)
    assert ranked["INC-B"].score == pytest.approx(0.5 * 1.0 + 0.2 * 1.0 + 0.3 * 0.5, abs=TOLERANCE)
    assert ranked["INC-B"].score == pytest.approx(0.85, abs=TOLERANCE)


def test_the_ranked_incident_keeps_the_descriptive_fields_of_the_candidate() -> None:
    # Arrange
    candidate = _candidate(
        "INC-7",
        title="Acquirer timeout",
        root_cause="Connection pool exhausted",
        fix_applied="Raised the pool size",
        started_at=TEST_NOW - timedelta(days=5),
        resolved_at=TEST_NOW - timedelta(days=4),
        recurrence_within_7d=True,
    )

    # Act
    (ranked,) = rerank_incidents([candidate], TEST_NOW)

    # Assert
    assert (
        ranked.incident_id,
        ranked.title,
        ranked.root_cause,
        ranked.fix_applied,
        ranked.started_at,
        ranked.resolved_at,
        ranked.recurrence_within_7d,
    ) == (
        "INC-7",
        "Acquirer timeout",
        "Connection pool exhausted",
        "Raised the pool size",
        TEST_NOW - timedelta(days=5),
        TEST_NOW - timedelta(days=4),
        True,
    )


def test_rerank_incidents_does_not_change_the_input_sequence() -> None:
    # Arrange
    candidates = [_candidate("INC-B"), _candidate("INC-A", helpful=5)]
    before = list(candidates)

    # Act
    rerank_incidents(candidates, TEST_NOW)

    # Assert
    assert candidates == before
