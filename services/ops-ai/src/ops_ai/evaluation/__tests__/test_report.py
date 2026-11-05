"""Evaluation report, JSON round trip, text rendering and CLI (docs/specs/evaluation-judge.md section 5)."""

import json
from pathlib import Path

import pytest
from ops_ai.evaluation.__main__ import main
from ops_ai.evaluation.golden import CaseResult
from ops_ai.evaluation.metrics import MetricResult, gate, not_applicable, skipped
from ops_ai.evaluation.report import EvalReport, render_text, report_from_json, report_to_json


def _case_a() -> CaseResult:
    return CaseResult(
        case_id="case_a",
        sample_query="SQ1",
        metrics=(
            gate("status", 1.0, 1.0),
            not_applicable("faithfulness", 0.9, "no narration"),
            skipped("llm_answer_relevancy", 0.7, "no OPENAI_API_KEY"),
        ),
    )


def _case_b(*, ok: bool) -> CaseResult:
    return CaseResult(
        case_id="case_b",
        sample_query="SQ4",
        metrics=(
            gate("answer_relevancy", 1.0 if ok else 2 / 3, 1.0, "2 of 3 checks"),
            gate("cross_tenant_leakage", 1.0, 1.0),
        ),
    )


def _suite(*, ok: bool) -> tuple[MetricResult, ...]:
    return (
        gate("fraud_f1", 1.0, 0.95),
        gate("fraud_recall", 1.0 if ok else 0.9, 0.95),
    )


def _report(*, case_ok: bool = True, suite_ok: bool = True) -> EvalReport:
    return EvalReport(cases=(_case_a(), _case_b(ok=case_ok)), suite_metrics=_suite(ok=suite_ok))


def test_eval_report_passes_when_no_case_and_no_suite_metric_failed() -> None:
    # Arrange
    report = _report()

    # Act
    outcome = (report.passed, report.failures())

    # Assert
    assert outcome == (True, ())


def test_eval_report_ignores_skipped_and_not_applicable_metrics() -> None:
    # Arrange
    report = EvalReport(
        cases=(_case_a(),),
        suite_metrics=(skipped("fraud_f1", 0.95, "no data"), not_applicable("fraud_recall", 0.95, "none")),
    )

    # Act
    outcome = (report.passed, report.failures())

    # Assert
    assert outcome == (True, ())


def test_eval_report_fails_and_names_the_failed_case_metric() -> None:
    # Arrange
    report = _report(case_ok=False)

    # Act
    outcome = (report.passed, report.failures())

    # Assert
    assert outcome == (False, ("case_b:answer_relevancy",))


def test_eval_report_fails_and_names_the_failed_suite_metric() -> None:
    # Arrange
    report = _report(suite_ok=False)

    # Act
    outcome = (report.passed, report.failures())

    # Assert
    assert outcome == (False, ("suite:fraud_recall",))


def test_eval_report_lists_case_failures_before_suite_failures() -> None:
    # Arrange
    report = _report(case_ok=False, suite_ok=False)

    # Act
    failures = report.failures()

    # Assert
    assert failures == ("case_b:answer_relevancy", "suite:fraud_recall")


def test_report_json_round_trip_preserves_the_report() -> None:
    # Arrange
    report = _report(case_ok=False, suite_ok=False)

    # Act
    restored = report_from_json(report_to_json(report))

    # Assert
    assert restored == report


def test_report_json_round_trip_preserves_none_scores_and_details() -> None:
    # Arrange
    report = _report()

    # Act
    restored = report_from_json(report_to_json(report))

    # Assert
    metrics = {metric.name: metric for metric in restored.cases[0].metrics}
    assert metrics["faithfulness"] == MetricResult(
        name="faithfulness", score=None, threshold=0.9, status="not_applicable", detail="no narration"
    )
    assert metrics["llm_answer_relevancy"].score is None


def test_report_to_json_is_valid_json_and_stable_across_a_round_trip() -> None:
    # Arrange
    report = _report(case_ok=False)

    # Act
    text = report_to_json(report)

    # Assert
    json.loads(text)
    assert report_to_json(report) == text
    assert report_to_json(report_from_json(text)) == text


def test_report_to_json_sorts_the_cases_by_case_id() -> None:
    # Arrange
    report = EvalReport(cases=(_case_b(ok=True), _case_a()), suite_metrics=())

    # Act
    restored = report_from_json(report_to_json(report))

    # Assert
    assert [case.case_id for case in restored.cases] == ["case_a", "case_b"]


def test_render_text_has_one_row_per_metric_with_scope_name_and_status() -> None:
    # Arrange
    report = _report(case_ok=False, suite_ok=False)
    expected_rows = [
        ("case_a", "status", "passed"),
        ("case_a", "faithfulness", "not_applicable"),
        ("case_a", "llm_answer_relevancy", "skipped"),
        ("case_b", "answer_relevancy", "failed"),
        ("case_b", "cross_tenant_leakage", "passed"),
        ("suite", "fraud_f1", "passed"),
        ("suite", "fraud_recall", "failed"),
    ]

    # Act
    lines = render_text(report).splitlines()

    # Assert
    for scope, name, status in expected_rows:
        rows = [line for line in lines if scope in line and name in line]
        assert len(rows) == 1, (scope, name)
        assert status in rows[0], (scope, name)


def test_main_show_returns_zero_and_prints_the_report_when_it_passed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    path = tmp_path / "report.json"
    path.write_text(report_to_json(_report()))

    # Act
    exit_code = main(["show", str(path)])

    # Assert
    output = capsys.readouterr().out
    assert exit_code == 0
    assert "answer_relevancy" in output
    assert "fraud_f1" in output


def test_main_show_returns_one_and_prints_the_report_when_it_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    path = tmp_path / "report.json"
    path.write_text(report_to_json(_report(case_ok=False, suite_ok=False)))

    # Act
    exit_code = main(["show", str(path)])

    # Assert
    output = capsys.readouterr().out
    assert exit_code == 1
    assert "answer_relevancy" in output
    assert "fraud_recall" in output
