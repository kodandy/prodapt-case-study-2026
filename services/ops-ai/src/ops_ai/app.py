import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from redis import Redis
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.agents.bus import RedisStreamTransport
from ops_ai.agents.routes import create_agents_router
from ops_ai.alerts.routes import create_alerts_router
from ops_ai.analytics.access import ForbiddenOpsError, OpsAccess, require_ops_principal
from ops_ai.analytics.routes import create_analytics_router
from ops_ai.assistant.citations import UnsupportedReason
from ops_ai.assistant.llm_port import LlmPort
from ops_ai.assistant.routes import create_assistant_router
from ops_ai.assistant.service import AnswerMode, GroundedAssistantAnswer, ask, ask_grounded
from ops_ai.assistant.tool_calling import ToolCallingLlm
from ops_ai.auth import InvalidTokenError, verify_token
from ops_ai.bulk.routes import create_bulk_router
from ops_ai.clock import Clock
from ops_ai.compliance.api import create_compliance_router
from ops_ai.feedback.routes import create_feedback_router
from ops_ai.fraud_watch import FraudAnalysis
from ops_ai.fraud_watch.service import FraudCause, FraudIntent, investigate_fraud
from ops_ai.investigation.playbook import Playbook, load_playbook
from ops_ai.investigation.service import CauseLabel, Intent, investigate
from ops_ai.investigation.tools import FailureAggregate
from ops_ai.judge.dispute_summary import DisputeRecordSource
from ops_ai.judge.root_cause import JUDGE_TIMEOUT_SECONDS, RootCauseAssessment, assess_root_cause
from ops_ai.judge.schemas import LlmStatus
from ops_ai.memory.routes import build_memory_router
from ops_ai.memory.session import SessionStore
from ops_ai.observability.http import install_observability
from ops_ai.scope import ForbiddenPrincipalError, OpsScope, Principal, Scope, UserScope, require_ops_role
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.incident_routes import create_incident_search_router
from ops_ai.settlement.service import explain_payout
from ops_ai.settlement.statement import LineKind
from ops_ai.tickets.api import create_ticket_router
from ops_ai.tickets.escalation import escalate_assistant_turn
from ops_ai.transfers import (
    CauseCode,
    RemediationOwner,
    SenderStatus,
    TransferStatus,
    sender_facing_status,
)
from ops_ai.transfers.service import investigate_transfer

logger = logging.getLogger(__name__)


def _always_ready() -> list[str]:
    return []


@dataclass(frozen=True)
class OpsAiAppDeps:
    session_factory: sessionmaker[Session]
    llm: LlmPort
    jwt_public_key_pem: str
    clock: Clock
    readiness: Callable[[], list[str]] = field(default=_always_ready)
    embedder: Embedder = field(default_factory=LocalHashEmbedder)
    redis: Redis | None = None
    """Set → the B10 session and memory routes are mounted (docs/specs/memory.md)."""
    tool_llm: ToolCallingLlm | None = None
    playbook: Playbook = field(default_factory=load_playbook)
    judge_llm: LlmPort | None = None
    """Set → `/v1/assistant/investigate?judge=true` assesses the root cause (evaluation-judge.md 6.4.2)."""
    judge_timeout_seconds: float = JUDGE_TIMEOUT_SECONDS
    dispute_records: DisputeRecordSource | None = None
    """Dispute workflow records for the dispute agent's judge (evaluation-judge.md 6.4.1)."""


class AskRequest(BaseModel):
    """No tenant field: the scope comes from the token only, so `merchant_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class EvidenceOut(BaseModel):
    ref: str
    summary: str


GROUNDED_ASK_BY_DEFAULT = False
"""R1-5: `?grounded=` default for /v1/assistant/ask. Flipping it makes the grounded answer the default."""


class SourceExcerptOut(BaseModel):
    ref: str
    excerpt: str


class CitedSentenceOut(BaseModel):
    sentence: str
    sources: list[SourceExcerptOut]


class UnsupportedSentenceOut(BaseModel):
    sentence: str
    reason: UnsupportedReason
    refs: list[str]


class AskResponse(BaseModel):
    """`grounded`, `citations`, `unsupported_sentences` and `relevant_refs` are present only with
    `?grounded=true` (R1-5); without it the body is exactly mode, text, model, evidence."""

    mode: AnswerMode
    text: str | None
    model: str | None
    evidence: list[EvidenceOut]
    grounded: bool = False
    citations: list[CitedSentenceOut] = Field(default_factory=list)
    unsupported_sentences: list[UnsupportedSentenceOut] = Field(default_factory=list)
    relevant_refs: list[str] = Field(default_factory=list)


class BinShareOut(BaseModel):
    card_bin: str
    failed: int
    total: int


class CodeCountOut(BaseModel):
    decline_code: str
    count: int


class AggregateOut(BaseModel):
    start: datetime
    end: datetime
    total: int
    failed: int
    rate: float
    by_bin: list[BinShareOut]
    by_code: list[CodeCountOut]

    @classmethod
    def of(cls, aggregate: FailureAggregate | None) -> "AggregateOut | None":
        if aggregate is None:
            return None
        return cls(
            start=aggregate.start,
            end=aggregate.end,
            total=aggregate.total,
            failed=aggregate.failed,
            rate=aggregate.rate,
            by_bin=[
                BinShareOut(card_bin=b.card_bin, failed=b.failed, total=b.total) for b in aggregate.by_bin
            ],
            by_code=[CodeCountOut(decline_code=c.decline_code, count=c.count) for c in aggregate.by_code],
        )


class ToolCallOut(BaseModel):
    name: str
    args: dict[str, str]


class InvestigateTransferRequest(BaseModel):
    """No identity field: the sender comes from the token only, so `user_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    transfer_id: uuid.UUID | None = None


class TransferOut(BaseModel):
    """No `rule_id` and no `reason_code`: the sender is told the numbers, not the external system's codes."""

    transfer_id: uuid.UUID
    amount_minor: int
    currency: str
    method: str
    status: SenderStatus
    country_sender: str
    country_receiver: str
    kyc_tier: int | None
    limit_minor: int | None
    limit_currency: str | None
    next_tier_limit_minor: int | None
    sender_balance_minor: int | None
    decided_at: datetime

    @classmethod
    def of(cls, transfer: TransferStatus) -> "TransferOut":
        return cls(
            transfer_id=transfer.transfer_id,
            amount_minor=transfer.amount_minor,
            currency=transfer.currency,
            method=transfer.method,
            status=sender_facing_status(transfer.status),
            country_sender=transfer.country_sender,
            country_receiver=transfer.country_receiver,
            kyc_tier=transfer.kyc_tier,
            limit_minor=transfer.limit_minor,
            limit_currency=transfer.limit_currency,
            next_tier_limit_minor=transfer.next_tier_limit_minor,
            sender_balance_minor=transfer.sender_balance_minor,
            decided_at=transfer.decided_at,
        )


class InvestigateTransferResponse(BaseModel):
    cause_code: CauseCode
    remediation_owner: RemediationOwner
    mode: AnswerMode
    text: str | None
    model: str | None
    transfer: TransferOut
    balance_covered_amount: bool | None
    suggested_fixes: list[str]
    tool_calls: list[ToolCallOut]
    evidence: list[EvidenceOut]


class RootCauseJudgeOut(BaseModel):
    status: str
    reason: str | None
    cause_code: str | None
    score: float | None
    passed: bool | None
    rationale: str | None
    resolution_steps: list[str]
    llm_status: LlmStatus | None
    model: str | None

    @classmethod
    def of(cls, assessment: RootCauseAssessment) -> "RootCauseJudgeOut":
        return cls(
            status=assessment.status,
            reason=assessment.reason,
            cause_code=assessment.cause_code,
            score=assessment.score,
            passed=assessment.passed,
            rationale=assessment.rationale,
            resolution_steps=list(assessment.resolution_steps),
            llm_status=assessment.llm_status,
            model=assessment.model,
        )


class InvestigateResponse(BaseModel):
    intent: Intent
    cause_label: CauseLabel
    mode: AnswerMode
    text: str | None
    model: str | None
    window: AggregateOut | None
    baseline: AggregateOut | None
    suggested_fixes: list[str]
    tool_calls: list[ToolCallOut]
    evidence: list[EvidenceOut]
    judge: RootCauseJudgeOut | None = None
    """Only with `?judge=true` (left unset otherwise, so the default response keeps its keys)."""


EPISODE_PAYMENT_IDS_LIMIT = 100
FRAUD_RULE_VERSION_HEADER = "X-Fraud-Rule-Version"


class EpisodeOut(BaseModel):
    first_payment_id: uuid.UUID
    start: datetime
    end: datetime
    payment_ids: list[uuid.UUID]
    """The first EPISODE_PAYMENT_IDS_LIMIT ids in (occurred_at, payment_id) order; micro_payments is the full
    count."""
    micro_payments: int
    card_payments: int
    micro_share: float
    distinct_cards: int | None
    declined: int
    decline_ratio: float
    card_bins: list[str]
    currencies: list[str]
    flagged: bool
    reasons: list[str]


class FraudAnalysisOut(BaseModel):
    start: datetime
    end: datetime
    micro_payments: int
    episodes: list[EpisodeOut]

    @classmethod
    def of(cls, analysis: FraudAnalysis | None) -> "FraudAnalysisOut | None":
        if analysis is None:
            return None
        return cls(
            start=analysis.start,
            end=analysis.end,
            micro_payments=analysis.micro_payments,
            episodes=[
                EpisodeOut(
                    first_payment_id=e.first_payment_id,
                    start=e.start,
                    end=e.end,
                    payment_ids=list(e.payment_ids[:EPISODE_PAYMENT_IDS_LIMIT]),
                    micro_payments=e.micro_payments,
                    card_payments=e.card_payments,
                    micro_share=e.micro_share,
                    distinct_cards=e.distinct_cards,
                    declined=e.declined,
                    decline_ratio=e.decline_ratio,
                    card_bins=list(e.card_bins),
                    currencies=list(e.currencies),
                    flagged=e.flagged,
                    reasons=list(e.reasons),
                )
                for e in analysis.episodes
            ],
        )


class AlertRefOut(BaseModel):
    alert_id: uuid.UUID
    ticket_id: uuid.UUID | None
    queue: str | None
    first_payment_id: uuid.UUID


class InvestigateFraudResponse(BaseModel):
    intent: FraudIntent
    cause_label: FraudCause
    mode: AnswerMode
    text: str | None
    model: str | None
    analysis: FraudAnalysisOut | None
    alerts: list[AlertRefOut]
    suggested_fixes: list[str]
    tool_calls: list[ToolCallOut]
    evidence: list[EvidenceOut]


# ASCII digits only: `\d` would also accept other scripts' digits.
StatementPeriod = Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")]


class ExplainSettlementRequest(BaseModel):
    """No tenant field: the merchant comes from the token only, so `merchant_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)
    period: StatementPeriod | None = None


class SettlementLineOut(BaseModel):
    kind: LineKind
    amount_minor: int
    count: int


class ExplainSettlementResponse(BaseModel):
    period: str
    currency: str
    contract_version: str
    fx_rate_id: str
    lines: list[SettlementLineOut]
    gross_minor: int
    expected_minor: int
    net_minor: int
    gap_minor: int
    verified: bool
    mismatches: list[str]
    projection_checked: int
    summary: str
    mode: AnswerMode
    text: str | None
    model: str | None
    tool_calls: list[ToolCallOut]
    evidence: list[EvidenceOut]


def create_app(deps: OpsAiAppDeps) -> FastAPI:
    """Routes:
    - GET /healthz → 200 (liveness). GET /readyz → 200 ready, or 503 with the failed components.
    - POST /v1/assistant/ask (Authorization: Bearer <merchant token>) {question} → AskResponse.
      `?grounded=true` (R1-5) adds per-sentence citations; see ops_ai.assistant.service.ask_grounded.
    - POST /v1/assistant/investigate (same auth and body as /ask) → InvestigateResponse; now = deps.clock().
    - POST /v1/assistant/investigate-transfer (Authorization: Bearer <user token>) {question, transfer_id?}
      → InvestigateTransferResponse with Cache-Control: no-store. Another sender's transfer and a missing one
      → the identical 404.
    - POST /v1/assistant/investigate-fraud (merchant token, same body as /ask) → InvestigateFraudResponse with
      Cache-Control: no-store and X-Fraud-Rule-Version: <applied version> (absent for unsupported questions);
      now = deps.clock().
    - POST /v1/assistant/explain-settlement (merchant token) {question, period?: YYYY-MM}
      → ExplainSettlementResponse with Cache-Control: no-store. No statement for the period → 404.
    - /ask also opens an escalation ticket when the assistant abstains or a human is asked for (B12.1).
    - GET /v1/tickets, GET /v1/tickets/{id}, POST /v1/tickets/{id}/transition (merchant token):
      see ops_ai.tickets.api.
    - GET /v1/ops/compliance/erasures (ops token): see ops_ai.compliance.api.
    - GET /v1/ops/analytics/{alerts,forecast,chargeback-risk} (ops token): see ops_ai.analytics.routes.
    Missing/invalid/expired token → 401 {"detail": "invalid token"}. A valid token of another kind (user or
    ops token on the merchant routes, merchant or ops token on the transfer route, a non-ops token on the ops
    routes) → 403 {"detail": "forbidden"}.
    """
    app = FastAPI(title="ops-ai")
    app.add_exception_handler(RequestValidationError, _validation_error_without_input)
    bearer = HTTPBearer(auto_error=False)

    def principal_from_token(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token")
        try:
            return verify_token(credentials.credentials, deps.jwt_public_key_pem, deps.clock())
        except InvalidTokenError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from None

    def merchant_scope(principal: Annotated[Principal, Depends(principal_from_token)]) -> Scope:
        if not isinstance(principal, Scope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return principal

    def user_scope(principal: Annotated[Principal, Depends(principal_from_token)]) -> UserScope:
        if not isinstance(principal, UserScope):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
        return principal

    def analytics_ops_access(principal: Annotated[Principal, Depends(principal_from_token)]) -> OpsAccess:
        try:
            return require_ops_principal(principal)
        except ForbiddenOpsError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from None

    def ops_scope(principal: Annotated[Principal, Depends(principal_from_token)]) -> OpsScope:
        try:
            return require_ops_role(principal)
        except ForbiddenPrincipalError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden") from None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        failed = deps.readiness()
        if failed:
            return JSONResponse({"status": "not_ready", "failed": failed}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/assistant/ask", response_model_exclude_unset=True)
    def assistant_ask(
        body: AskRequest,
        scope: Annotated[Scope, Depends(merchant_scope)],
        grounded: bool = GROUNDED_ASK_BY_DEFAULT,
    ) -> AskResponse:
        grounded_result: GroundedAssistantAnswer | None = None
        with deps.session_factory() as session:
            if grounded:
                grounded_result = ask_grounded(session, scope, body.question, deps.llm, deps.embedder)
                answer = grounded_result.answer
            else:
                answer = ask(session, scope, body.question, deps.llm, deps.embedder)
        try:
            with deps.session_factory.begin() as session:
                escalate_assistant_turn(session, deps.clock, scope, body.question, answer.mode)
        except SQLAlchemyError as error:
            # The answer is already built; a failed escalation is logged by class name only (no question).
            logger.warning("assistant escalation failed: %s", type(error).__name__)
        evidence = [EvidenceOut(ref=item.ref, summary=item.summary) for item in answer.evidence]
        if grounded_result is None:
            return AskResponse(mode=answer.mode, text=answer.text, model=answer.model, evidence=evidence)
        grounding = grounded_result.grounding
        return AskResponse(
            mode=answer.mode,
            text=answer.text,
            model=answer.model,
            evidence=evidence,
            grounded=grounding is not None and grounding.grounded,
            citations=[]
            if grounding is None
            else [
                CitedSentenceOut(
                    sentence=cited.sentence,
                    sources=[SourceExcerptOut(ref=s.ref, excerpt=s.excerpt) for s in cited.sources],
                )
                for cited in grounding.citations
            ],
            unsupported_sentences=[]
            if grounding is None
            else [
                UnsupportedSentenceOut(sentence=u.sentence, reason=u.reason, refs=list(u.refs))
                for u in grounding.unsupported
            ],
            relevant_refs=list(grounded_result.relevant_refs),
        )

    @app.post("/v1/assistant/investigate", response_model_exclude_unset=True)
    def assistant_investigate(
        body: AskRequest, scope: Annotated[Scope, Depends(merchant_scope)], judge: bool = False
    ) -> InvestigateResponse:
        with deps.session_factory() as session:
            result = investigate(session, scope, body.question, deps.clock(), deps.llm, deps.embedder)
        extra: dict[str, RootCauseJudgeOut] = {}
        if judge:
            assessment = assess_root_cause(result, deps.judge_llm, deps.judge_timeout_seconds)
            extra["judge"] = RootCauseJudgeOut.of(assessment)
        return InvestigateResponse(
            **extra,
            intent=result.intent,
            cause_label=result.cause_label,
            mode=result.mode,
            text=result.text,
            model=result.model,
            window=AggregateOut.of(result.window),
            baseline=AggregateOut.of(result.baseline),
            suggested_fixes=list(result.suggested_fixes),
            tool_calls=[ToolCallOut(name=call.name, args=call.args) for call in result.tool_calls],
            evidence=[EvidenceOut(ref=item.ref, summary=item.summary) for item in result.evidence],
        )

    @app.post("/v1/assistant/investigate-transfer")
    def assistant_investigate_transfer(
        body: InvestigateTransferRequest,
        scope: Annotated[UserScope, Depends(user_scope)],
        response: Response,
    ) -> InvestigateTransferResponse:
        with deps.session_factory() as session:
            result = investigate_transfer(session, scope, body.question, body.transfer_id, deps.llm)
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "transfer not found")
        response.headers["Cache-Control"] = "no-store"
        return InvestigateTransferResponse(
            cause_code=result.cause_code,
            remediation_owner=result.remediation_owner,
            mode=result.mode,
            text=result.text,
            model=result.model,
            transfer=TransferOut.of(result.transfer),
            balance_covered_amount=result.balance_covered_amount,
            suggested_fixes=list(result.suggested_fixes),
            tool_calls=[ToolCallOut(name=call.name, args=call.args) for call in result.tool_calls],
            evidence=[EvidenceOut(ref=item.ref, summary=item.summary) for item in result.evidence],
        )

    @app.post("/v1/assistant/investigate-fraud")
    def assistant_investigate_fraud(
        body: AskRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> InvestigateFraudResponse:
        with deps.session_factory() as session:
            result = investigate_fraud(session, scope, body.question, deps.clock(), deps.llm)
        response.headers["Cache-Control"] = "no-store"
        if result.rule_version is not None:
            response.headers[FRAUD_RULE_VERSION_HEADER] = str(result.rule_version)
        return InvestigateFraudResponse(
            intent=result.intent,
            cause_label=result.cause_label,
            mode=result.mode,
            text=result.text,
            model=result.model,
            analysis=FraudAnalysisOut.of(result.analysis),
            alerts=[
                AlertRefOut(
                    alert_id=a.alert_id,
                    ticket_id=a.ticket_id,
                    queue=a.queue,
                    first_payment_id=a.first_payment_id,
                )
                for a in result.alerts
            ],
            suggested_fixes=list(result.suggested_fixes),
            tool_calls=[ToolCallOut(name=call.name, args=call.args) for call in result.tool_calls],
            evidence=[EvidenceOut(ref=item.ref, summary=item.summary) for item in result.evidence],
        )

    @app.post("/v1/assistant/explain-settlement")
    def assistant_explain_settlement(
        body: ExplainSettlementRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> ExplainSettlementResponse:
        with deps.session_factory() as session:
            result = explain_payout(session, scope, body.question, body.period, deps.llm)
        if result is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "settlement not found")
        response.headers["Cache-Control"] = "no-store"
        statement = result.statement
        return ExplainSettlementResponse(
            period=statement.period,
            currency=statement.currency,
            contract_version=statement.contract_version,
            fx_rate_id=statement.fx_rate_id,
            lines=[
                SettlementLineOut(kind=line.kind, amount_minor=line.amount_minor, count=line.count)
                for line in statement.lines
            ],
            gross_minor=statement.gross_minor,
            expected_minor=statement.expected_minor,
            net_minor=statement.net_minor,
            gap_minor=statement.gap_minor,
            verified=statement.verified,
            mismatches=list(statement.mismatches),
            projection_checked=statement.projection_checked,
            summary=result.summary,
            mode=result.mode,
            text=result.text,
            model=result.model,
            tool_calls=[ToolCallOut(name=call.name, args=call.args) for call in result.tool_calls],
            evidence=[EvidenceOut(ref=item.ref, summary=item.summary) for item in result.evidence],
        )

    if deps.redis is not None:
        app.include_router(
            build_memory_router(
                session_factory=deps.session_factory,
                store=SessionStore(deps.redis),
                llm=deps.llm,
                clock=deps.clock,
                merchant_scope=merchant_scope,
            )
        )

    app.include_router(create_assistant_router(deps, merchant_scope, principal_from_token))
    app.include_router(create_ticket_router(deps.session_factory, deps.clock, merchant_scope))
    app.include_router(
        create_analytics_router(deps.session_factory, deps.clock, merchant_scope, analytics_ops_access)
    )
    app.include_router(create_compliance_router(deps.session_factory, ops_scope))

    def ops_staff_id(scope: Annotated[OpsScope, Depends(ops_scope)]) -> uuid.UUID:
        return scope.ops_user_id

    app.include_router(create_alerts_router(deps.session_factory, deps.clock, ops_staff_id))
    app.include_router(create_feedback_router(deps.session_factory, deps.clock, merchant_scope))
    app.include_router(create_bulk_router(deps.session_factory, deps.llm, merchant_scope))
    app.include_router(
        create_incident_search_router(deps.session_factory, deps.clock, deps.embedder, merchant_scope)
    )
    app.include_router(
        create_agents_router(
            deps.session_factory,
            deps.llm,
            deps.clock,
            merchant_scope,
            user_scope,
            None if deps.redis is None else RedisStreamTransport(deps.redis),
            dispute_records=deps.dispute_records,
        )
    )
    install_observability(app)
    return app


async def _validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's default 422 echoes the rejected input back."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in errors]
    return JSONResponse({"error": "validation_failed", "detail": detail}, status_code=422)
