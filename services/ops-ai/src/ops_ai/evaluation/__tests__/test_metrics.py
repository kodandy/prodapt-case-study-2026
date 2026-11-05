"""Metric results and deterministic response-quality metrics (docs/specs/evaluation-judge.md section 1)."""

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.evaluation.metrics import (
    MetricResult,
    answer_relevancy,
    cited_refs,
    evidence_ref_coverage,
    faithfulness,
    gate,
    not_applicable,
    skipped,
)

WINDOW = EvidenceItem(ref="aggregate:window", summary="Window: 40 card payments, 36 failed")


def test_gate_passes_when_the_score_equals_the_threshold() -> None:
    # Arrange
    expected = MetricResult(name="m", score=0.9, threshold=0.9, status="passed", detail="d")

    # Act
    result = gate("m", 0.9, 0.9, "d")

    # Assert
    assert result == expected


def test_gate_passes_when_the_score_is_above_the_threshold() -> None:
    # Arrange / Act
    result = gate("m", 1.0, 0.95)

    # Assert
    assert result == MetricResult(name="m", score=1.0, threshold=0.95, status="passed", detail="")


def test_gate_fails_when_the_score_is_below_the_threshold() -> None:
    # Arrange / Act
    result = gate("m", 0.89, 0.9, "too low")

    # Assert
    assert result == MetricResult(name="m", score=0.89, threshold=0.9, status="failed", detail="too low")


def test_skipped_has_no_score_and_the_skipped_status() -> None:
    # Arrange / Act
    result = skipped("llm_faithfulness", 0.7, "no OPENAI_API_KEY")

    # Assert
    assert result == MetricResult(
        name="llm_faithfulness", score=None, threshold=0.7, status="skipped", detail="no OPENAI_API_KEY"
    )


def test_not_applicable_has_no_score_and_the_not_applicable_status() -> None:
    # Arrange / Act
    result = not_applicable("faithfulness", 0.9, "no narration")

    # Assert
    assert result == MetricResult(
        name="faithfulness", score=None, threshold=0.9, status="not_applicable", detail="no narration"
    )


def test_metric_result_is_frozen() -> None:
    # Arrange
    result = gate("m", 1.0, 1.0)

    # Act / Assert
    with pytest.raises(AttributeError):
        result.score = 0.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("no citations here", ()),
        ("See [aggregate:window] and [incident:inc_0001].", ("aggregate:window", "incident:inc_0001")),
        ("[a] then [b] then [a] again", ("a", "b", "a")),
        ("adjacent [a][b]", ("a", "b")),
        ("empty brackets [] are not a ref", ()),
        ("whitespace [two words] is not a ref", ()),
        ("nested [[inner]] keeps the innermost", ("inner",)),
        ("a uuid [payment:0f8fad5b-d9cb-469f-a165]", ("payment:0f8fad5b-d9cb-469f-a165",)),
        ("", ()),
    ],
)
def test_cited_refs_returns_every_bracketed_ref_in_order_of_appearance(
    text: str, expected: tuple[str, ...]
) -> None:
    # Arrange / Act
    refs = cited_refs(text)

    # Assert
    assert refs == expected


def test_faithfulness_is_none_when_the_text_is_none() -> None:
    # Arrange / Act
    score = faithfulness(None, [WINDOW])

    # Assert
    assert score is None


@pytest.mark.parametrize("text", ["", "Nothing numeric or cited to report."])
def test_faithfulness_is_one_when_the_text_has_no_claims(text: str) -> None:
    # Arrange / Act
    score = faithfulness(text, [])

    # Assert
    assert score == 1.0


def test_faithfulness_is_one_when_every_ref_and_number_is_supported() -> None:
    # Arrange
    text = "The window had 40 payments [aggregate:window]."

    # Act
    score = faithfulness(text, [WINDOW])

    # Assert
    assert score == 1.0


def test_faithfulness_counts_a_fabricated_ref_as_an_unsupported_claim() -> None:
    # Arrange: claims = ref ok, ref fabricated, number 40 ok
    text = "40 failures [aggregate:window] [payment:fabricated]"

    # Act
    score = faithfulness(text, [WINDOW])

    # Assert
    assert score == pytest.approx(2 / 3)


def test_faithfulness_counts_an_unsupported_number_as_an_unsupported_claim() -> None:
    # Arrange: claims = ref ok, number 40 ok, number 99 unsupported
    text = "40 failed [aggregate:window], 99 declined"

    # Act
    score = faithfulness(text, [WINDOW])

    # Assert
    assert score == pytest.approx(2 / 3)


def test_faithfulness_is_the_supported_fraction_of_refs_and_numbers_together() -> None:
    # Arrange: claims = ref ok, ref fabricated, 40 ok, 99 unsupported
    text = "40 failed [aggregate:window] [payment:fabricated], 99 declined"

    # Act
    score = faithfulness(text, [WINDOW])

    # Assert
    assert score == 0.5


def test_faithfulness_is_zero_when_the_only_cited_ref_is_not_in_the_evidence() -> None:
    # Arrange
    text = "Something happened [incident:inc_0001]"

    # Act
    score = faithfulness(text, [])

    # Assert
    assert score == 0.0


def test_faithfulness_matches_refs_exactly_not_by_prefix() -> None:
    # Arrange
    evidence = [EvidenceItem(ref="payment:12", summary="a payment")]

    # Act
    score = faithfulness("see [payment:1]", evidence)

    # Assert
    assert score == 0.0


def test_faithfulness_supports_a_number_that_is_only_in_the_question() -> None:
    # Arrange
    text = "15 payments failed"

    # Act
    with_question = faithfulness(text, [], "Why did 15 payments fail?")
    without_question = faithfulness(text, [])

    # Assert
    assert (with_question, without_question) == (1.0, 0.0)


def test_faithfulness_supports_a_number_that_is_only_in_an_evidence_ref() -> None:
    # Arrange
    evidence = [EvidenceItem(ref="aggregate:bin:400000", summary="failures on one BIN")]

    # Act
    score = faithfulness("BIN 400000 is failing [aggregate:bin:400000]", evidence)

    # Assert
    assert score == 1.0


def test_faithfulness_does_not_count_the_digits_inside_a_citation_as_a_separate_number() -> None:
    # Arrange: 400000 occurs only inside the citation and in no summary
    evidence = [EvidenceItem(ref="aggregate:bin:400000", summary="failures on one BIN")]

    # Act
    score = faithfulness("[aggregate:bin:400000]", evidence)

    # Assert
    assert score == 1.0


def test_faithfulness_matches_numbers_as_whole_tokens() -> None:
    # Arrange
    evidence = [EvidenceItem(ref="aggregate:window", summary="Failed 150 times")]

    # Act
    score = faithfulness("15 failed", evidence)

    # Assert
    assert score == 0.0


def test_faithfulness_treats_a_decimal_as_one_number_token() -> None:
    # Arrange
    evidence = [EvidenceItem(ref="aggregate:window", summary="rate 12.5 percent")]

    # Act
    supported = faithfulness("rate 12.5", evidence)
    unsupported = faithfulness("rate 12", evidence)

    # Assert
    assert (supported, unsupported) == (1.0, 0.0)


def test_evidence_ref_coverage_is_the_fraction_of_prefixes_matching_some_ref() -> None:
    # Arrange
    refs = ["aggregate:window", "incident:inc_0001"]
    prefixes = ["aggregate:window", "incident:", "payment:"]

    # Act
    score = evidence_ref_coverage(refs, prefixes)

    # Assert
    assert score == pytest.approx(2 / 3)


def test_evidence_ref_coverage_is_one_when_every_prefix_is_covered() -> None:
    # Arrange
    refs = ["aggregate:window", "payment:abc", "payment:def"]

    # Act
    score = evidence_ref_coverage(refs, ["aggregate:", "payment:"])

    # Assert
    assert score == 1.0


def test_evidence_ref_coverage_is_zero_when_there_are_no_refs() -> None:
    # Arrange / Act
    score = evidence_ref_coverage([], ["aggregate:window"])

    # Assert
    assert score == 0.0


def test_evidence_ref_coverage_is_one_when_there_are_no_expected_prefixes() -> None:
    # Arrange / Act
    score = evidence_ref_coverage([], [])

    # Assert
    assert score == 1.0


def test_answer_relevancy_is_one_when_every_key_and_term_matches() -> None:
    # Arrange
    observed: dict[str, object] = {"intent": "failure_spike", "verified": True}
    expected: dict[str, object] = {"intent": "failure_spike", "verified": True}

    # Act
    score = answer_relevancy(observed, expected, "BIN 400000 is failing", ["400000"])

    # Assert
    assert score == 1.0


def test_answer_relevancy_fails_the_check_of_a_key_with_a_different_value() -> None:
    # Arrange
    observed: dict[str, object] = {"intent": "failure_spike", "n": 1}
    expected: dict[str, object] = {"intent": "failure_spike", "n": 2}

    # Act
    score = answer_relevancy(observed, expected, "", [])

    # Assert
    assert score == 0.5


def test_answer_relevancy_fails_the_check_of_a_key_missing_from_observed() -> None:
    # Arrange
    expected: dict[str, object] = {"intent": "failure_spike"}

    # Act
    score = answer_relevancy({}, expected, "", [])

    # Assert
    assert score == 0.0


def test_answer_relevancy_passes_an_expected_none_for_a_missing_key() -> None:
    # Arrange: the spec compares observed.get(key) with the value, so a missing key reads as None
    expected: dict[str, object] = {"cause": None}

    # Act
    score = answer_relevancy({}, expected, "", [])

    # Assert
    assert score == 1.0


def test_answer_relevancy_matches_a_required_term_case_insensitively() -> None:
    # Arrange / Act
    score = answer_relevancy({}, {}, "The ISSUER behind BIN 400000 timed out", ["issuer", "Bin"])

    # Assert
    assert score == 1.0


def test_answer_relevancy_fails_the_check_of_a_term_missing_from_the_text() -> None:
    # Arrange / Act
    score = answer_relevancy({}, {}, "The issuer timed out", ["issuer", "acquirer"])

    # Assert
    assert score == 0.5


def test_answer_relevancy_is_the_passed_fraction_of_key_and_term_checks_together() -> None:
    # Arrange: checks = key ok, key wrong, term ok, term missing
    observed: dict[str, object] = {"a": 1, "b": 2}
    expected: dict[str, object] = {"a": 1, "b": 3}

    # Act
    score = answer_relevancy(observed, expected, "hello world", ["hello", "absent"])

    # Assert
    assert score == 0.5


def test_answer_relevancy_is_a_third_when_one_of_three_checks_passes() -> None:
    # Arrange
    observed: dict[str, object] = {"a": 1}
    expected: dict[str, object] = {"a": 1, "b": 2}

    # Act
    score = answer_relevancy(observed, expected, "", ["missing"])

    # Assert
    assert score == pytest.approx(1 / 3)


def test_answer_relevancy_is_one_when_there_are_no_checks() -> None:
    # Arrange / Act
    score = answer_relevancy({"anything": 1}, {}, "any text", [])

    # Assert
    assert score == 1.0
