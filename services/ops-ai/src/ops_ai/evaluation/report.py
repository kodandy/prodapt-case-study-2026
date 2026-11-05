"""Evaluation report: gate outcome, JSON round trip and text table (docs/specs/evaluation-judge.md s5)."""

import json
from dataclasses import asdict, dataclass

from pydantic import BaseModel, ConfigDict

from ops_ai.evaluation.golden import CaseResult
from ops_ai.evaluation.metrics import MetricResult, MetricStatus

SUITE_SCOPE = "suite"


@dataclass(frozen=True)
class EvalReport:
    cases: tuple[CaseResult, ...]
    suite_metrics: tuple[MetricResult, ...]

    def failures(self) -> tuple[str, ...]:
        case_failures = [
            f"{case.case_id}:{metric.name}"
            for case in self.cases
            for metric in case.metrics
            if metric.status == "failed"
        ]
        suite_failures = [f"{SUITE_SCOPE}:{m.name}" for m in self.suite_metrics if m.status == "failed"]
        return (*case_failures, *suite_failures)

    @property
    def passed(self) -> bool:
        return not self.failures()


class _MetricFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    score: float | None
    threshold: float
    status: MetricStatus
    detail: str


class _CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    case_id: str
    sample_query: str
    metrics: list[_MetricFile]


class _ReportFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    cases: list[_CaseFile]
    suite_metrics: list[_MetricFile]


def _metric(raw: _MetricFile) -> MetricResult:
    return MetricResult(
        name=raw.name, score=raw.score, threshold=raw.threshold, status=raw.status, detail=raw.detail
    )


def report_to_json(report: EvalReport) -> str:
    payload = {
        "cases": [asdict(case) for case in sorted(report.cases, key=lambda case: case.case_id)],
        "suite_metrics": [asdict(metric) for metric in report.suite_metrics],
    }
    return json.dumps(payload, sort_keys=True, indent=2) + "\n"


def report_from_json(text: str) -> EvalReport:
    parsed = _ReportFile.model_validate_json(text)
    return EvalReport(
        cases=tuple(
            CaseResult(
                case_id=case.case_id,
                sample_query=case.sample_query,
                metrics=tuple(_metric(metric) for metric in case.metrics),
            )
            for case in parsed.cases
        ),
        suite_metrics=tuple(_metric(metric) for metric in parsed.suite_metrics),
    )


def _row(scope: str, metric: MetricResult) -> str:
    score = "-" if metric.score is None else f"{metric.score:.4f}"
    row = f"{scope:<36} {metric.name:<32} {score:>8} {metric.threshold:>9.4f}  {metric.status}"
    return f"{row}  {metric.detail}" if metric.detail else row


def render_text(report: EvalReport) -> str:
    header = f"{'scope':<36} {'metric':<32} {'score':>8} {'threshold':>9}  status"
    rows = [_row(case.case_id, metric) for case in report.cases for metric in case.metrics]
    rows.extend(_row(SUITE_SCOPE, metric) for metric in report.suite_metrics)
    failed = len(report.failures())
    verdict = "PASSED" if failed == 0 else f"FAILED ({failed} failed gate(s))"
    return "\n".join((header, *rows, f"eval gate {verdict}"))
