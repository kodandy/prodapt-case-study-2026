"""End to end evaluation gate over the generated dataset (docs/specs/evaluation-judge.md section 5)."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.db import make_session_factory
from ops_ai.evaluation.golden import CaseResult, load_golden_cases
from ops_ai.evaluation.report import EvalReport, report_to_json
from ops_ai.evaluation.suite import run_full_eval
from sqlalchemy import text
from sqlalchemy.engine import Engine

from conftest import DATA_SCHEMAS

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
GOLDEN_DIR = Path(__file__).resolve().parents[4] / "eval" / "golden"
SUITE_METRIC_NAMES = [
    "fraud_precision",
    "fraud_recall",
    "fraud_f1",
    "failure_dataset_accuracy",
    "failure_dataset_macro_f1",
    "failure_paraphrase_accuracy",
    "judge_dispute_agreement",
    "judge_ranking_agreement",
]
SUITE_THRESHOLDS = {
    "fraud_precision": 0.95,
    "fraud_recall": 0.95,
    "fraud_f1": 0.95,
    "failure_dataset_accuracy": 0.99,
    "failure_dataset_macro_f1": 0.99,
    "failure_paraphrase_accuracy": 0.75,
    "judge_dispute_agreement": 1.0,
    "judge_ranking_agreement": 1.0,
}
LLM_METRIC_NAMES = ("llm_faithfulness", "llm_answer_relevancy")


@pytest.fixture(scope="module")
def report(engine: Engine) -> EvalReport:
    """Runs the whole evaluation once per module and writes the report before any assertion reads it."""
    # Arrange
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) AND tablename <> 'alembic_version'"
            ),
            {"schemas": list(DATA_SCHEMAS)},
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    session_factory = make_session_factory(engine)

    # Act
    result = run_full_eval(session_factory, ANCHOR, MockLlm(), None)
    report_path = os.environ.get("EVAL_REPORT_PATH", "")
    if report_path:
        Path(report_path).write_text(report_to_json(result))
    return result


def _case(report: EvalReport, case_id: str) -> CaseResult:
    return next(case for case in report.cases if case.case_id == case_id)


def test_full_eval_has_no_failed_gate(report: EvalReport) -> None:
    # Arrange / Act
    failures = report.failures()

    # Assert
    assert failures == ()
    assert report.passed is True


def test_full_eval_scores_every_golden_case(report: EvalReport) -> None:
    # Arrange
    expected = {case.case_id for case in load_golden_cases(GOLDEN_DIR)}

    # Act
    scored = [case.case_id for case in report.cases]

    # Assert
    assert len(scored) == len(expected)
    assert set(scored) == expected


def test_full_eval_has_no_cross_tenant_leak_in_any_case(report: EvalReport) -> None:
    # Arrange / Act
    leakage = {
        case.case_id: next(m.status for m in case.metrics if m.name == "cross_tenant_leakage")
        for case in report.cases
    }

    # Assert
    assert leakage == {case.case_id: "passed" for case in report.cases}


def test_full_eval_status_metric_passes_in_every_case(report: EvalReport) -> None:
    # Arrange / Act
    statuses = {
        case.case_id: next(m.status for m in case.metrics if m.name == "status") for case in report.cases
    }

    # Assert
    assert statuses == {case.case_id: "passed" for case in report.cases}


def test_full_eval_sq1_case_ranks_the_hypotheses_in_the_expected_order(report: EvalReport) -> None:
    # Arrange
    case = _case(report, "sq1_bin_outage")

    # Act
    ranking = next(metric for metric in case.metrics if metric.name == "hypothesis_ranking")

    # Assert
    assert (ranking.score, ranking.status) == (1.0, "passed")


def test_full_eval_skips_the_llm_judged_metrics_without_a_judge(report: EvalReport) -> None:
    # Arrange
    narrated = [case for case in report.cases if case.case_id != "isolation_user_on_merchant_route"]

    # Act
    statuses = {
        (case.case_id, metric.name): metric.status
        for case in narrated
        for metric in case.metrics
        if metric.name in LLM_METRIC_NAMES
    }

    # Assert
    assert len(statuses) == 2 * len(narrated)
    assert set(statuses.values()) == {"skipped"}


def test_full_eval_never_runs_the_llm_judged_metrics_on_the_forbidden_case(report: EvalReport) -> None:
    # Arrange
    case = _case(report, "isolation_user_on_merchant_route")

    # Act
    statuses = {metric.status for metric in case.metrics if metric.name in LLM_METRIC_NAMES}

    # Assert
    assert statuses <= {"skipped", "not_applicable"}
    assert len([metric for metric in case.metrics if metric.name in LLM_METRIC_NAMES]) == 2


def test_full_eval_suite_metrics_are_the_eight_specified_gates_and_all_pass(report: EvalReport) -> None:
    # Arrange / Act
    names = [metric.name for metric in report.suite_metrics]
    statuses = {metric.name: metric.status for metric in report.suite_metrics}

    # Assert
    assert names == SUITE_METRIC_NAMES
    assert statuses == {name: "passed" for name in SUITE_METRIC_NAMES}


def test_full_eval_suite_metrics_use_the_specified_thresholds(report: EvalReport) -> None:
    # Arrange / Act
    thresholds = {metric.name: metric.threshold for metric in report.suite_metrics}

    # Assert
    assert thresholds == SUITE_THRESHOLDS


def test_full_eval_fraud_recall_is_one_and_the_paraphrase_accuracy_is_twenty_of_twenty_six(
    report: EvalReport,
) -> None:
    # Arrange / Act
    scores = {metric.name: metric.score for metric in report.suite_metrics}

    # Assert
    assert scores["fraud_recall"] == 1.0
    assert scores["failure_dataset_accuracy"] == 1.0
    assert scores["failure_paraphrase_accuracy"] == pytest.approx(20 / 26)
