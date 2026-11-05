import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.feedback.service import (
    FeedbackRejectedError,
    IncidentScore,
    TargetKind,
    Verdict,
    record_feedback,
)
from ops_ai.feedback.thresholds import (
    RuleAuditEntry,
    RuleVersion,
    active_rule_version,
    revert_rule,
    rule_audit,
    rule_history,
)
from ops_ai.scope import Scope

NO_STORE = {"Cache-Control": "no-store"}
_REJECTIONS = {
    "verdict_mismatch": (status.HTTP_422_UNPROCESSABLE_ENTITY, "verdict does not match target"),
    "unknown_target": (status.HTTP_404_NOT_FOUND, "target not found"),
    "duplicate": (status.HTTP_409_CONFLICT, "feedback already recorded"),
}


class FeedbackRequest(BaseModel):
    """No tenant field: the merchant comes from the token only, so `merchant_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    target_kind: TargetKind
    target_id: Annotated[StrictStr, Field(min_length=1, max_length=128)]
    verdict: Verdict
    comment: Annotated[StrictStr, Field(max_length=500)] | None = None


class RevertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[StrictInt, Field(ge=0)]


class RuleVersionOut(BaseModel):
    version: int
    steps: int
    min_micro_payments: int
    min_micro_share: float
    min_distinct_cards: int
    reason: str
    feedback_id: uuid.UUID | None
    created_at: datetime | None

    @classmethod
    def of(cls, rule: RuleVersion) -> "RuleVersionOut":
        return cls(
            version=rule.version,
            steps=rule.steps,
            min_micro_payments=rule.min_micro_payments,
            min_micro_share=rule.min_micro_share,
            min_distinct_cards=rule.min_distinct_cards,
            reason=rule.reason,
            feedback_id=rule.feedback_id,
            created_at=rule.created_at,
        )


class IncidentScoreOut(BaseModel):
    incident_id: str
    helpful: int
    not_helpful: int

    @classmethod
    def of(cls, score: IncidentScore | None) -> "IncidentScoreOut | None":
        if score is None:
            return None
        return cls(incident_id=score.incident_id, helpful=score.helpful, not_helpful=score.not_helpful)


class FeedbackOut(BaseModel):
    feedback_id: uuid.UUID
    target_kind: TargetKind
    target_id: str
    verdict: Verdict
    rule_version: RuleVersionOut | None
    incident_score: IncidentScoreOut | None


class FraudRulesOut(BaseModel):
    active: RuleVersionOut
    history: list[RuleVersionOut]


class RuleAuditEntryOut(BaseModel):
    version: int
    reason: str
    feedback_id: uuid.UUID | None
    actor: str | None
    actor_role: str | None
    created_at: datetime

    @classmethod
    def of(cls, entry: RuleAuditEntry) -> "RuleAuditEntryOut":
        return cls(
            version=entry.version,
            reason=entry.reason,
            feedback_id=entry.feedback_id,
            actor=entry.actor,
            actor_role=entry.actor_role,
            created_at=entry.created_at,
        )


class FraudRuleAuditOut(BaseModel):
    entries: list[RuleAuditEntryOut]


def _rejected(reason: str) -> HTTPException:
    code, detail = _REJECTIONS[reason]
    return HTTPException(code, detail, headers=NO_STORE)


def create_feedback_router(
    session_factory: sessionmaker[Session], clock: Clock, merchant_scope: Callable[..., Scope]
) -> APIRouter:
    """POST /v1/feedback, GET /v1/feedback/fraud-rules, GET /v1/feedback/fraud-rules/audit,
    POST /v1/feedback/fraud-rules/revert (merchant token, the merchant's own feedback and rules only)."""
    router = APIRouter()

    @router.post("/v1/feedback", status_code=status.HTTP_201_CREATED)
    def post_feedback(
        body: FeedbackRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> FeedbackOut:
        try:
            with session_factory.begin() as session:
                result = record_feedback(
                    session, scope, clock, body.target_kind, body.target_id, body.verdict, body.comment
                )
        except FeedbackRejectedError as error:
            raise _rejected(error.reason) from None
        except IntegrityError:
            # A concurrent vote on the same target (or a concurrent rule version) lost the race.
            raise _rejected("duplicate") from None
        response.headers["Cache-Control"] = "no-store"
        return FeedbackOut(
            feedback_id=result.feedback_id,
            target_kind=result.target_kind,
            target_id=result.target_id,
            verdict=result.verdict,
            rule_version=None if result.rule_version is None else RuleVersionOut.of(result.rule_version),
            incident_score=IncidentScoreOut.of(result.incident_score),
        )

    @router.get("/v1/feedback/fraud-rules")
    def get_fraud_rules(
        scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> FraudRulesOut:
        with session_factory() as session:
            active = active_rule_version(session, scope)
            history = rule_history(session, scope)
        response.headers["Cache-Control"] = "no-store"
        return FraudRulesOut(
            active=RuleVersionOut.of(active), history=[RuleVersionOut.of(rule) for rule in history]
        )

    @router.get("/v1/feedback/fraud-rules/audit")
    def get_fraud_rule_audit(
        scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> FraudRuleAuditOut:
        with session_factory() as session:
            entries = rule_audit(session, scope)
        response.headers["Cache-Control"] = "no-store"
        return FraudRuleAuditOut(entries=[RuleAuditEntryOut.of(entry) for entry in entries])

    @router.post("/v1/feedback/fraud-rules/revert", status_code=status.HTTP_201_CREATED)
    def post_revert(
        body: RevertRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> RuleVersionOut:
        try:
            with session_factory.begin() as session:
                reverted = revert_rule(session, scope, clock, body.version)
        except IntegrityError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "rule changed concurrently", headers=NO_STORE
            ) from None
        if reverted is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "rule version not found", headers=NO_STORE)
        response.headers["Cache-Control"] = "no-store"
        return RuleVersionOut.of(reverted)

    return router
