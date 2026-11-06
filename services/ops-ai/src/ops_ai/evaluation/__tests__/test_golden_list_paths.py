"""List indices in golden field paths (docs/specs/sq1.md section 11.1)."""

from pathlib import Path

import pytest
from ops_ai.evaluation.golden import GoldenCase, load_golden_cases, score_case

GOLDEN_DIR = Path(__file__).resolve().parents[4] / "eval" / "golden"
FOREIGN = frozenset({"mch_foreign"})
# Relevancy averages the field check with the tool-call check,
# which always passes here (none expected, none made).
FIELD_MISSED_SCORE = 0.5


def _case(fields: dict[str, object]) -> GoldenCase:
    return GoldenCase.model_validate(
        {
            "case_id": "list_paths",
            "sample_query": "SQ1",
            "route": "/v1/assistant/failures",
            "principal": "sq1_merchant",
            "body": {"question": "why are card payments failing?"},
            "expect": {"status": 200, "fields": fields},
        }
    )


def _body() -> dict[str, object]:
    return {
        "abstained": False,
        "comparison": {
            "elevated": True,
            "window": {"by_bin": [{"card_bin": "400000"}, {"card_bin": "411111"}]},
        },
        "reasons": [{"taxonomy_code": "issuer_unavailable"}, {"taxonomy_code": "expired_card"}],
        "fixes": [],
        "evidence": [],
        "tool_calls": [],
    }


def _relevancy(fields: dict[str, object]) -> float | None:
    result = score_case(_case(fields), 200, _body(), FOREIGN)
    return next(metric.score for metric in result.metrics if metric.name == "answer_relevancy")


def test_numeric_segment_indexes_into_a_list() -> None:
    # Arrange
    fields: dict[str, object] = {
        "reasons.0.taxonomy_code": "issuer_unavailable",
        "reasons.1.taxonomy_code": "expired_card",
        "comparison.window.by_bin.0.card_bin": "400000",
    }

    # Act
    score = _relevancy(fields)

    # Assert
    assert score == 1.0


def test_list_index_compares_the_value_at_that_position() -> None:
    # Arrange
    fields: dict[str, object] = {
        "reasons.0.taxonomy_code": "expired_card",
        "reasons.1.taxonomy_code": "expired_card",
    }

    # Act
    score = _relevancy(fields)

    # Assert: the second position matches, the first does not; the tool-call check passes.
    assert score == 2 / 3


@pytest.mark.parametrize(
    "path",
    [
        "reasons.2.taxonomy_code",
        "reasons.-1.taxonomy_code",
        "reasons.first.taxonomy_code",
        "fixes.0.taxonomy_code",
        "comparison.0.elevated",
    ],
)
def test_unresolvable_list_path_counts_as_missing_without_raising(path: str) -> None:
    # Arrange
    fields: dict[str, object] = {path: None}

    # Act
    score = _relevancy(fields)

    # Assert
    assert score == FIELD_MISSED_SCORE


def test_mapping_paths_resolve_as_before() -> None:
    # Arrange
    fields: dict[str, object] = {"abstained": False, "comparison.elevated": True}

    # Act
    score = _relevancy(fields)

    # Assert
    assert score == 1.0


def test_sq1_failure_route_golden_targets_the_failure_route_with_list_paths() -> None:
    # Arrange / Act
    case = next(case for case in load_golden_cases(GOLDEN_DIR) if case.case_id == "sq1_failure_route")

    # Assert
    assert (case.route, case.principal, case.sample_query) == (
        "/v1/assistant/failures",
        "sq1_merchant",
        "SQ1",
    )
    assert case.expect.fields["reasons.0.taxonomy_code"] == "issuer_unavailable"
    assert case.expect.fields["fixes.0.taxonomy_code"] == "issuer_unavailable"
    assert case.expect.tool_calls == (
        "compare_baseline",
        "change_timeline",
        "failure_reasons",
        "search_incidents",
        "list_failed_payments",
    )
    assert not any(prefix.startswith("incident:") for prefix in case.expect.evidence_ref_prefixes)
