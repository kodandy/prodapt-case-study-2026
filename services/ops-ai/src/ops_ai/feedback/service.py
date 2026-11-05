"""Ops feedback on alerts, answers and incidents (R2-7).

One vote per (merchant, target) bounds how far feedback can move anything. Alert feedback moves the
merchant's fraud thresholds one step; incident feedback updates the merchant's effectiveness counts used by
reranking; answer feedback is stored for evaluation only.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ops_ai.clock import Clock
from ops_ai.feedback.models import Feedback, IncidentFeedbackScore
from ops_ai.feedback.thresholds import (
    RuleVersion,
    active_rule_version,
    append_rule_version,
    clamp_steps,
    thresholds_for_steps,
)
from ops_ai.fraud_watch.models import FraudAlert
from ops_ai.investigation.models import IncidentRecord
from ops_ai.scope import Scope

TargetKind = Literal["alert", "answer", "incident"]
Verdict = Literal["true_positive", "false_positive", "helpful", "not_helpful"]
RejectionReason = Literal["verdict_mismatch", "unknown_target", "duplicate"]

VERDICTS_BY_KIND: Mapping[TargetKind, tuple[Verdict, Verdict]] = {
    "alert": ("true_positive", "false_positive"),
    "answer": ("helpful", "not_helpful"),
    "incident": ("helpful", "not_helpful"),
}
_STEP_DELTA: Mapping[Verdict, int] = {"false_positive": 1, "true_positive": -1}


@dataclass(frozen=True)
class IncidentScore:
    incident_id: str
    helpful: int
    not_helpful: int


class FeedbackRejectedError(Exception):
    def __init__(self, reason: RejectionReason) -> None:
        super().__init__(reason)
        self.reason: RejectionReason = reason


@dataclass(frozen=True)
class FeedbackResult:
    feedback_id: uuid.UUID
    target_kind: TargetKind
    target_id: str
    verdict: Verdict
    rule_version: RuleVersion | None
    incident_score: IncidentScore | None


def _alert_visible(session: Session, scope: Scope, target_id: str) -> bool:
    try:
        alert_id = uuid.UUID(target_id)
    except ValueError:
        return False
    found = session.execute(
        select(FraudAlert.alert_id).where(
            FraudAlert.alert_id == alert_id, FraudAlert.merchant_id == scope.merchant_id
        )
    ).first()
    return found is not None


def _incident_visible(session: Session, scope: Scope, target_id: str) -> bool:
    found = session.execute(
        select(IncidentRecord.incident_id).where(
            IncidentRecord.incident_id == target_id,
            or_(IncidentRecord.merchant_id == scope.merchant_id, IncidentRecord.merchant_id.is_(None)),
        )
    ).first()
    return found is not None


def _target_visible(session: Session, scope: Scope, target_kind: TargetKind, target_id: str) -> bool:
    if target_kind == "alert":
        return _alert_visible(session, scope, target_id)
    if target_kind == "incident":
        return _incident_visible(session, scope, target_id)
    return True


def _already_recorded(session: Session, scope: Scope, target_kind: TargetKind, target_id: str) -> bool:
    found = session.execute(
        select(Feedback.feedback_id).where(
            Feedback.merchant_id == scope.merchant_id,
            Feedback.target_kind == target_kind,
            Feedback.target_id == target_id,
        )
    ).first()
    return found is not None


def _move_thresholds(
    session: Session, scope: Scope, clock: Clock, verdict: Verdict, feedback_id: uuid.UUID
) -> RuleVersion | None:
    active = active_rule_version(session, scope)
    steps = clamp_steps(active.steps + _STEP_DELTA[verdict])
    if steps == active.steps:
        return None
    return append_rule_version(
        session, scope, clock, steps, thresholds_for_steps(steps), f"feedback:{verdict}", feedback_id
    )


def _bump_incident_score(
    session: Session, scope: Scope, clock: Clock, incident_id: str, verdict: Verdict
) -> IncidentScore:
    helpful, not_helpful = (1, 0) if verdict == "helpful" else (0, 1)
    score = IncidentFeedbackScore
    statement = (
        insert(score)
        .values(
            merchant_id=scope.merchant_id,
            incident_id=incident_id,
            helpful=helpful,
            not_helpful=not_helpful,
            updated_at=clock(),
        )
        .on_conflict_do_update(
            index_elements=[score.merchant_id, score.incident_id],
            set_={
                "helpful": score.helpful + helpful,
                "not_helpful": score.not_helpful + not_helpful,
                "updated_at": clock(),
            },
        )
        .returning(score.helpful, score.not_helpful)
    )
    row = session.execute(statement).one()
    return IncidentScore(incident_id=incident_id, helpful=row.helpful, not_helpful=row.not_helpful)


def record_feedback(
    session: Session,
    scope: Scope,
    clock: Clock,
    target_kind: TargetKind,
    target_id: str,
    verdict: Verdict,
    comment: str | None,
) -> FeedbackResult:
    """Stores one verdict and applies its effect. Raises FeedbackRejectedError. Does not commit."""
    if verdict not in VERDICTS_BY_KIND[target_kind]:
        raise FeedbackRejectedError("verdict_mismatch")
    if not _target_visible(session, scope, target_kind, target_id):
        raise FeedbackRejectedError("unknown_target")
    if _already_recorded(session, scope, target_kind, target_id):
        raise FeedbackRejectedError("duplicate")
    feedback_id = uuid.uuid4()
    session.add(
        Feedback(
            feedback_id=feedback_id,
            merchant_id=scope.merchant_id,
            target_kind=target_kind,
            target_id=target_id,
            verdict=verdict,
            comment=comment,
            created_at=clock(),
        )
    )
    session.flush()
    rule_version: RuleVersion | None = None
    incident_score: IncidentScore | None = None
    if target_kind == "alert":
        rule_version = _move_thresholds(session, scope, clock, verdict, feedback_id)
    elif target_kind == "incident":
        incident_score = _bump_incident_score(session, scope, clock, target_id, verdict)
    return FeedbackResult(
        feedback_id=feedback_id,
        target_kind=target_kind,
        target_id=target_id,
        verdict=verdict,
        rule_version=rule_version,
        incident_score=incident_score,
    )


def incident_scores(session: Session, scope: Scope, incident_ids: Sequence[str]) -> dict[str, IncidentScore]:
    if not incident_ids:
        return {}
    rows = session.execute(
        select(IncidentFeedbackScore).where(
            IncidentFeedbackScore.merchant_id == scope.merchant_id,
            IncidentFeedbackScore.incident_id.in_(list(incident_ids)),
        )
    ).scalars()
    return {
        row.incident_id: IncidentScore(
            incident_id=row.incident_id, helpful=row.helpful, not_helpful=row.not_helpful
        )
        for row in rows
    }
