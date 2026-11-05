"""Binary and multiclass classification reports (docs/specs/evaluation-judge.md section 1.2)."""

import pytest
from ops_ai.evaluation.classification import (
    BinaryReport,
    ClassScore,
    binary_report,
    multiclass_report,
)

UNIVERSE = frozenset({1, 2, 3, 4})


def _assert_binary(
    report: BinaryReport,
    counts: tuple[int, int, int, int],
    scores: tuple[float, float, float, float],
) -> None:
    """counts = (tp, fp, fn, tn); scores = (precision, recall, f1, accuracy)."""
    observed = (report.true_positive, report.false_positive, report.false_negative, report.true_negative)
    assert observed == counts
    assert report.precision == pytest.approx(scores[0])
    assert report.recall == pytest.approx(scores[1])
    assert report.f1 == pytest.approx(scores[2])
    assert report.accuracy == pytest.approx(scores[3])


def _assert_class(score: ClassScore, precision: float, recall: float, f1: float, support: int) -> None:
    assert score.precision == pytest.approx(precision)
    assert score.recall == pytest.approx(recall)
    assert score.f1 == pytest.approx(f1)
    assert score.support == support


def test_binary_report_counts_and_scores_a_mixed_prediction() -> None:
    # Arrange: tp {2,3}, fp {1}, fn {4,5}, tn {6..10}
    universe = frozenset(range(1, 11))

    # Act
    report = binary_report(frozenset({1, 2, 3}), frozenset({2, 3, 4, 5}), universe)

    # Assert
    _assert_binary(report, (2, 1, 2, 5), (2 / 3, 0.5, 4 / 7, 0.7))


def test_binary_report_is_perfect_when_predicted_equals_actual() -> None:
    # Arrange
    positives = frozenset({1, 2})

    # Act
    report = binary_report(positives, positives, UNIVERSE)

    # Assert
    _assert_binary(report, (2, 0, 0, 2), (1.0, 1.0, 1.0, 1.0))


def test_binary_report_is_perfect_when_nothing_is_predicted_and_nothing_is_actual() -> None:
    # Arrange / Act
    report = binary_report(frozenset(), frozenset(), UNIVERSE)

    # Assert
    _assert_binary(report, (0, 0, 0, 4), (1.0, 1.0, 1.0, 1.0))


def test_binary_report_precision_is_zero_when_nothing_is_predicted_but_a_positive_is_missed() -> None:
    # Arrange / Act
    report = binary_report(frozenset(), frozenset({1}), UNIVERSE)

    # Assert
    _assert_binary(report, (0, 0, 1, 3), (0.0, 0.0, 0.0, 0.75))


def test_binary_report_recall_is_one_when_there_is_no_actual_positive() -> None:
    # Arrange / Act
    report = binary_report(frozenset({1}), frozenset(), UNIVERSE)

    # Assert
    _assert_binary(report, (0, 1, 0, 3), (0.0, 1.0, 0.0, 0.75))


def test_binary_report_f1_is_zero_when_precision_and_recall_are_both_zero() -> None:
    # Arrange / Act
    report = binary_report(frozenset({1}), frozenset({2}), UNIVERSE)

    # Assert
    _assert_binary(report, (0, 1, 1, 2), (0.0, 0.0, 0.0, 0.5))


def test_binary_report_predicting_the_whole_universe_gives_low_precision_and_full_recall() -> None:
    # Arrange / Act
    report = binary_report(UNIVERSE, frozenset({1}), UNIVERSE)

    # Assert
    _assert_binary(report, (1, 3, 0, 0), (0.25, 1.0, 0.4, 0.25))


def test_binary_report_rejects_a_prediction_outside_the_universe() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        binary_report(frozenset({99}), frozenset({1}), UNIVERSE)


def test_binary_report_rejects_an_actual_outside_the_universe() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        binary_report(frozenset({1}), frozenset({99}), UNIVERSE)


def test_binary_report_rejects_an_empty_universe() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        binary_report(frozenset(), frozenset(), frozenset())


def test_multiclass_report_scores_each_class_and_the_confusion_matrix() -> None:
    # Arrange: expected a x3, b x2, c x1; predicted a x4, b x2, c x0
    pairs = [("a", "a"), ("a", "a"), ("a", "b"), ("b", "b"), ("b", "a"), ("c", "a")]

    # Act
    report = multiclass_report(pairs)

    # Assert
    assert (report.total, report.correct, report.accuracy) == (6, 3, 0.5)
    assert report.macro_f1 == pytest.approx(5 / 14)
    assert set(report.per_class) == {"a", "b", "c"}
    _assert_class(report.per_class["a"], 0.5, 2 / 3, 4 / 7, 3)
    _assert_class(report.per_class["b"], 0.5, 0.5, 0.5, 2)
    _assert_class(report.per_class["c"], 0.0, 0.0, 0.0, 1)
    expected_confusion = {("a", "a"): 2, ("a", "b"): 1, ("b", "b"): 1, ("b", "a"): 1, ("c", "a"): 1}
    assert dict(report.confusion) == expected_confusion


def test_multiclass_report_includes_a_class_that_is_only_ever_predicted() -> None:
    # Arrange
    pairs = [("a", "a"), ("a", "z")]

    # Act
    report = multiclass_report(pairs)

    # Assert
    assert set(report.per_class) == {"a", "z"}
    _assert_class(report.per_class["a"], 1.0, 0.5, 2 / 3, 2)
    _assert_class(report.per_class["z"], 0.0, 1.0, 0.0, 0)
    assert report.macro_f1 == pytest.approx(1 / 3)
    assert dict(report.confusion) == {("a", "a"): 1, ("a", "z"): 1}


def test_multiclass_report_is_perfect_when_every_pair_matches() -> None:
    # Arrange
    pairs = [("a", "a"), ("b", "b"), ("b", "b")]

    # Act
    report = multiclass_report(pairs)

    # Assert
    assert (report.total, report.correct, report.accuracy, report.macro_f1) == (3, 3, 1.0, 1.0)
    assert dict(report.confusion) == {("a", "a"): 1, ("b", "b"): 2}
    _assert_class(report.per_class["b"], 1.0, 1.0, 1.0, 2)


def test_multiclass_report_confusion_keeps_only_non_zero_cells() -> None:
    # Arrange
    pairs = [("a", "b"), ("b", "a")]

    # Act
    report = multiclass_report(pairs)

    # Assert
    assert dict(report.confusion) == {("a", "b"): 1, ("b", "a"): 1}
    assert (report.correct, report.accuracy, report.macro_f1) == (0, 0.0, 0.0)


def test_multiclass_report_rejects_empty_pairs() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        multiclass_report([])
