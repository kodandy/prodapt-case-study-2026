"""Payment failure prediction accuracy (docs/specs/evaluation-judge.md section 2.2)."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from ops_ai.evaluation.classification import MulticlassReport, multiclass_report
from ops_ai.investigation.taxonomy import failure_code
from scripts.generate_dataset import ScenarioLog

LABEL_FILE_VERSION = 1


@dataclass(frozen=True)
class LabeledReason:
    text: str
    expected: str


@dataclass(frozen=True)
class FailureLabelSet:
    reason_catalog: Mapping[str, str]
    cases: tuple[LabeledReason, ...]


class _CaseFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: StrictStr
    expected: StrictStr


class _LabelFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: StrictInt
    reason_catalog: dict[StrictStr, StrictStr]
    cases: list[_CaseFile]


def load_failure_labels(path: Path) -> FailureLabelSet:
    try:
        parsed = _LabelFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (ValidationError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid failure label file {path.name}") from exc
    if parsed.version != LABEL_FILE_VERSION:
        raise ValueError(f"unsupported failure label file version {parsed.version}")
    return FailureLabelSet(
        reason_catalog=dict(parsed.reason_catalog),
        cases=tuple(LabeledReason(text=case.text, expected=case.expected) for case in parsed.cases),
    )


def dataset_failure_cases(log: ScenarioLog, catalog: Mapping[str, str]) -> tuple[LabeledReason, ...]:
    """Every failed or flagged merchant card payment, labelled through the catalog."""
    cases: list[LabeledReason] = []
    for tx in log.transactions:
        if tx.merchant_id is None or tx.payment_method != "card" or tx.status not in ("failed", "flagged"):
            continue
        reason = tx.failure_reason or ""
        if reason not in catalog:
            raise ValueError(f"failure reason missing from the catalog: {reason!r}")
        cases.append(LabeledReason(text=reason, expected=catalog[reason]))
    return tuple(cases)


def failure_prediction_report(
    cases: Sequence[LabeledReason], classify: Callable[[str], str] = failure_code
) -> MulticlassReport:
    return multiclass_report([(case.expected, classify(case.text)) for case in cases])
