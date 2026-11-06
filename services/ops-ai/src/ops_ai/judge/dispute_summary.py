"""The dispute agent's call into the dispute judge (docs/specs/evaluation-judge.md section 6.4.1)."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from ops_ai.judge.dispute import judge_dispute
from ops_ai.judge.schemas import DisputeVerdict, DisputeWorkflowRecord, Verdict
from ops_ai.scope import Scope

__all__ = [
    "MAX_JUDGED_DISPUTES",
    "DisputeJudgement",
    "DisputeJudgementStatus",
    "DisputeRecordSource",
    "SessionBindableDisputeRecordSource",
    "judge_merchant_disputes",
]

MAX_JUDGED_DISPUTES = 20
_MAX_RATIONALE = 300
_SEVERITY: dict[Verdict, int] = {"valid": 0, "needs_review": 1, "invalid": 2}

DisputeJudgementStatus = Literal["judged", "skipped"]


class DisputeRecordSource(Protocol):
    def dispute_records(self, scope: Scope, limit: int) -> Sequence[DisputeWorkflowRecord]: ...


@runtime_checkable
class SessionBindableDisputeRecordSource(Protocol):
    """A DB-backed source that can read through the caller's session, so one investigate request holds one
    pooled connection instead of checking out a second one per request."""

    def bound(self, session: Session) -> DisputeRecordSource: ...


@dataclass(frozen=True)
class DisputeJudgement:
    status: DisputeJudgementStatus
    reason: str | None
    disputes: int
    verdict: Verdict | None
    score: float | None
    passed: bool | None
    rationale: str | None
    dispute_ids: tuple[uuid.UUID, ...]


def _skipped(reason: str) -> DisputeJudgement:
    return DisputeJudgement(
        status="skipped",
        reason=reason,
        disputes=0,
        verdict=None,
        score=None,
        passed=None,
        rationale=None,
        dispute_ids=(),
    )


def _rationale(verdicts: Sequence[DisputeVerdict]) -> str:
    failing = next((item for item in verdicts if item.verdict != "valid"), None)
    if failing is None:
        return f"{len(verdicts)} dispute workflow(s) pass the rubric"
    details = "; ".join(reason.detail for reason in failing.reasons)
    return f"dispute {failing.dispute_id}: {details}"[:_MAX_RATIONALE]


def judge_merchant_disputes(scope: Scope, source: DisputeRecordSource | None) -> DisputeJudgement:
    """Rubric-only verdicts on the merchant's own dispute workflows; never raises."""
    if source is None:
        return _skipped("no dispute workflow source is configured")
    try:
        records = list(source.dispute_records(scope, MAX_JUDGED_DISPUTES))
    except Exception:
        return _skipped("dispute workflow records unavailable")
    own = [record for record in records if record.merchant_id == scope.merchant_id][:MAX_JUDGED_DISPUTES]
    if not own:
        return _skipped("no dispute workflow record for this merchant")
    verdicts = [judge_dispute(record) for record in own]
    worst = max((item.verdict for item in verdicts), key=_SEVERITY.__getitem__)
    score = sum(sum(check.passed for check in item.checks) / len(item.checks) for item in verdicts) / len(
        verdicts
    )
    return DisputeJudgement(
        status="judged",
        reason=None,
        disputes=len(verdicts),
        verdict=worst,
        score=round(score, 4),
        passed=worst == "valid",
        rationale=_rationale(verdicts),
        dispute_ids=tuple(record.dispute_id for record in own),
    )
