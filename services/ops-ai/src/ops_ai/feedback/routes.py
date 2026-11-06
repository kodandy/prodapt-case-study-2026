import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.feedback.paging import (
    DEFAULT_PAGE_SIZE,
    MAX_CURSOR_LENGTH,
    MAX_PAGE_SIZE,
    CursorKind,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
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
    rule_audit_page,
    rule_history_page,
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


class FraudRulesPageOut(FraudRulesOut):
    """`next_cursor` is left unset (omitted) on an unpaged request whose first page is the last (spec 7.3)."""

    next_cursor: str | None = None


class FraudRuleAuditPageOut(FraudRuleAuditOut):
    next_cursor: str | None = None


PageLimit = Annotated[int | None, Query(ge=1, le=MAX_PAGE_SIZE)]
PageCursor = Annotated[str | None, Query(min_length=1, max_length=MAX_CURSOR_LENGTH)]


def _rejected(reason: str) -> HTTPException:
    code, detail = _REJECTIONS[reason]
    return HTTPException(code, detail, headers=NO_STORE)


def _before(cursor: str | None, kind: CursorKind, scope: Scope) -> int | None:
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor, kind, scope.merchant_id)
    except InvalidCursorError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "invalid cursor", headers=NO_STORE
        ) from None


def _carries_next_cursor(next_before: int | None, limit: int | None, cursor: str | None) -> bool:
    return next_before is not None or limit is not None or cursor is not None


def _next_cursor(next_before: int | None, kind: CursorKind, scope: Scope) -> str | None:
    return None if next_before is None else encode_cursor(kind, scope.merchant_id, next_before)


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

    @router.get(
        "/v1/feedback/fraud-rules", response_model=FraudRulesPageOut, response_model_exclude_unset=True
    )
    def get_fraud_rules(
        scope: Annotated[Scope, Depends(merchant_scope)],
        response: Response,
        limit: PageLimit = None,
        cursor: PageCursor = None,
    ) -> FraudRulesPageOut:
        before = _before(cursor, "history", scope)
        with session_factory() as session:
            active = active_rule_version(session, scope)
            page = rule_history_page(session, scope, limit or DEFAULT_PAGE_SIZE, before)
        response.headers["Cache-Control"] = "no-store"
        out = FraudRulesPageOut(
            active=RuleVersionOut.of(active), history=[RuleVersionOut.of(rule) for rule in page.items]
        )
        if _carries_next_cursor(page.next_before, limit, cursor):
            out.next_cursor = _next_cursor(page.next_before, "history", scope)
        return out

    @router.get(
        "/v1/feedback/fraud-rules/audit",
        response_model=FraudRuleAuditPageOut,
        response_model_exclude_unset=True,
    )
    def get_fraud_rule_audit(
        scope: Annotated[Scope, Depends(merchant_scope)],
        response: Response,
        limit: PageLimit = None,
        cursor: PageCursor = None,
    ) -> FraudRuleAuditPageOut:
        before = _before(cursor, "audit", scope)
        with session_factory() as session:
            page = rule_audit_page(session, scope, limit or DEFAULT_PAGE_SIZE, before)
        response.headers["Cache-Control"] = "no-store"
        out = FraudRuleAuditPageOut(entries=[RuleAuditEntryOut.of(entry) for entry in page.items])
        if _carries_next_cursor(page.next_before, limit, cursor):
            out.next_cursor = _next_cursor(page.next_before, "audit", scope)
        return out

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
