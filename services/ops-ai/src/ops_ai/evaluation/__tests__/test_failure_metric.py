"""Payment failure prediction accuracy (docs/specs/evaluation-judge.md section 2.2)."""

import json
import re
from collections import Counter
from pathlib import Path

import pytest
from ops_ai.evaluation.failure import (
    FailureLabelSet,
    LabeledReason,
    dataset_failure_cases,
    failure_prediction_report,
    load_failure_labels,
)

from scripts.generate_dataset import ScenarioLog, generate

FAILURE_LABELS_PATH = Path(__file__).resolve().parents[4] / "eval" / "failure_labels.json"
MISSED_PARAPHRASES = frozenset(
    {
        "Authorization request timed out at the issuer",
        "issuer down / not responding",
        "Not enough balance on card",
        "The card expiry date has passed",
        "Security code (CVV) mismatch",
        "Risk engine blocked the payment",
    }
)


@pytest.fixture(scope="module")
def labels() -> FailureLabelSet:
    return load_failure_labels(FAILURE_LABELS_PATH)


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate(seed=42)


def _failed_card_reasons(log: ScenarioLog) -> list[str]:
    reasons: list[str] = []
    for tx in log.transactions:
        if tx.merchant_id is not None and tx.payment_method == "card" and tx.status in ("failed", "flagged"):
            assert tx.failure_reason is not None
            reasons.append(tx.failure_reason)
    return reasons


def test_load_failure_labels_reads_the_shipped_label_file(labels: FailureLabelSet) -> None:
    # Arrange / Act
    catalog_size = len(labels.reason_catalog)
    case_count = len(labels.cases)

    # Assert
    assert (catalog_size, case_count) == (10, 26)
    assert labels.reason_catalog["Card expired"] == "expired_card"
    assert labels.reason_catalog["Issuer unavailable (timeout) for BIN 400000"] == "issuer_unavailable"
    assert labels.cases[0] == LabeledReason(
        text="Issuer unavailable, please retry later", expected="issuer_unavailable"
    )
    assert labels.cases[-1] == LabeledReason(
        text="Amount exceeds the card's per-transaction limit", expected="other"
    )


def test_load_failure_labels_reads_a_minimal_file(tmp_path: Path) -> None:
    # Arrange
    path = tmp_path / "labels.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "reason_catalog": {"Card expired": "expired_card"},
                "cases": [{"text": "card has expired", "expected": "expired_card"}],
            }
        )
    )

    # Act
    result = load_failure_labels(path)

    # Assert
    assert dict(result.reason_catalog) == {"Card expired": "expired_card"}
    assert result.cases == (LabeledReason(text="card has expired", expected="expired_card"),)


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "reason_catalog": {}, "cases": []},
        {"reason_catalog": {}, "cases": []},
        {"version": 1, "cases": []},
        {"version": 1, "reason_catalog": {}},
        {"version": 1, "reason_catalog": [], "cases": []},
        {"version": 1, "reason_catalog": {"a": 1}, "cases": []},
        {"version": 1, "reason_catalog": {}, "cases": {}},
        {"version": 1, "reason_catalog": {}, "cases": ["text only"]},
        {"version": 1, "reason_catalog": {}, "cases": [{"text": "no expected"}]},
        {"version": 1, "reason_catalog": {}, "cases": [{"expected": "other"}]},
        {"version": 1, "reason_catalog": {}, "cases": [{"text": 1, "expected": "other"}]},
        ["not", "an", "object"],
    ],
)
def test_load_failure_labels_rejects_a_wrong_version_or_shape(tmp_path: Path, payload: object) -> None:
    # Arrange
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(payload))

    # Act / Assert
    with pytest.raises(ValueError):
        load_failure_labels(path)


def test_dataset_failure_cases_labels_every_failed_card_payment_with_its_catalog_code(
    log: ScenarioLog, labels: FailureLabelSet
) -> None:
    # Arrange
    reasons = _failed_card_reasons(log)
    expected = Counter((reason, labels.reason_catalog[reason]) for reason in reasons)
    non_card_failures = sum(
        1
        for tx in log.transactions
        if tx.status in ("failed", "flagged") and (tx.merchant_id is None or tx.payment_method != "card")
    )

    # Act
    cases = dataset_failure_cases(log, labels.reason_catalog)

    # Assert
    assert non_card_failures > 0
    assert len(cases) == len(reasons)
    assert Counter((case.text, case.expected) for case in cases) == expected


def test_dataset_failure_cases_only_uses_codes_from_the_catalog(
    log: ScenarioLog, labels: FailureLabelSet
) -> None:
    # Arrange
    allowed = set(labels.reason_catalog.values())

    # Act
    cases = dataset_failure_cases(log, labels.reason_catalog)

    # Assert
    assert {case.expected for case in cases} <= allowed
    assert all(case.text in labels.reason_catalog for case in cases)


def test_dataset_failure_cases_rejects_a_reason_missing_from_the_catalog_and_names_it(
    log: ScenarioLog, labels: FailureLabelSet
) -> None:
    # Arrange
    missing = _failed_card_reasons(log)[0]
    catalog = {reason: code for reason, code in labels.reason_catalog.items() if reason != missing}

    # Act / Assert
    with pytest.raises(ValueError, match=re.escape(missing)):
        dataset_failure_cases(log, catalog)


def test_failure_prediction_report_is_perfect_on_the_dataset_cases_with_the_default_classifier(
    log: ScenarioLog, labels: FailureLabelSet
) -> None:
    # Arrange
    cases = dataset_failure_cases(log, labels.reason_catalog)

    # Act
    report = failure_prediction_report(cases)

    # Assert
    assert report.total == len(cases)
    assert report.correct == len(cases)
    assert report.accuracy == 1.0
    assert report.macro_f1 == 1.0


def test_failure_prediction_report_gets_exactly_20_of_26_paraphrases_right(labels: FailureLabelSet) -> None:
    # Arrange
    cases = labels.cases

    # Act
    report = failure_prediction_report(cases)

    # Assert
    assert (report.total, report.correct) == (26, 20)
    assert report.accuracy == pytest.approx(20 / 26)
    assert report.accuracy >= 0.75


def test_failure_prediction_report_confusion_lists_exactly_the_six_missed_paraphrases(
    labels: FailureLabelSet,
) -> None:
    # Arrange
    assert {case.text for case in labels.cases} >= MISSED_PARAPHRASES

    # Act
    report = failure_prediction_report(labels.cases)

    # Assert
    off_diagonal = {cell: count for cell, count in report.confusion.items() if cell[0] != cell[1]}
    assert off_diagonal == {
        ("issuer_unavailable", "other"): 2,
        ("insufficient_funds", "other"): 1,
        ("expired_card", "other"): 1,
        ("incorrect_cvc", "other"): 1,
        ("fraud_flagged", "other"): 1,
    }


def test_failure_prediction_report_uses_the_injected_classifier() -> None:
    # Arrange
    cases = [
        LabeledReason(text="first", expected="other"),
        LabeledReason(text="second", expected="expired_card"),
        LabeledReason(text="third", expected="other"),
    ]
    seen: list[str] = []

    def always_other(text: str) -> str:
        seen.append(text)
        return "other"

    # Act
    report = failure_prediction_report(cases, always_other)

    # Assert
    assert sorted(seen) == ["first", "second", "third"]
    assert (report.total, report.correct) == (3, 2)
    assert report.accuracy == pytest.approx(2 / 3)
    assert dict(report.confusion) == {("other", "other"): 2, ("expired_card", "other"): 1}


def test_failure_prediction_report_with_an_oracle_classifier_is_perfect_on_the_paraphrases(
    labels: FailureLabelSet,
) -> None:
    # Arrange
    answers = {case.text: case.expected for case in labels.cases}

    # Act
    report = failure_prediction_report(labels.cases, answers.__getitem__)

    # Assert
    assert (report.accuracy, report.macro_f1) == (1.0, 1.0)


def test_failure_prediction_report_rejects_no_cases() -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError):
        failure_prediction_report([])
