import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.assistant.llm_port import LlmPort
from ops_ai.assistant.service import AnswerMode, ask
from ops_ai.auth import InvalidTokenError, verify_token
from ops_ai.clock import Clock
from ops_ai.fraud_watch import FraudAnalysis
from ops_ai.fraud_watch.service import FraudCause, FraudIntent, investigate_fraud
from ops_ai.investigation.service import CauseLabel, Intent, investigate
from ops_ai.investigation.tools import FailureAggregate
from ops_ai.scope import Principal, Scope, UserScope
from ops_ai.transfers import (
    CauseCode,
    RemediationOwner,
    SenderStatus,
    TransferStatus,
    sender_facing_status,
)
from ops_ai.transfers.service import investigate_transfer


def _always_ready() -> list[str]:
    return []


@dataclass(frozen=True)
class OpsAiAppDeps:
    session_factory: sessionmaker[Session]
    llm: LlmPort
    jwt_public_key_pem: str
    clock: Clock
    readiness: Callable[[], list[str]] = field(default=_always_ready)


class AskRequest(BaseModel):
    """No tenant field: the scope comes from the token only, so `merchant_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class EvidenceOut(BaseModel):
    ref: str
    summary: str


class AskResponse(BaseModel):
    mode: AnswerMode
    text: str | None
    model: str | None
    evidence: list[EvidenceOut]


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


EPISODE_PAYMENT_IDS_LIMIT = 100


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


def create_app(deps: OpsAiAppDeps) -> FastAPI:
    """Routes:
    - GET /healthz → 200 (liveness). GET /readyz → 200 ready, or 503 with the failed components.
    - POST /v1/assistant/ask (Authorization: Bearer <merchant token>) {question} → AskResponse.
    - POST /v1/assistant/investigate (same auth and body as /ask) → InvestigateResponse; now = deps.clock().
    - POST /v1/assistant/investigate-transfer (Authorization: Bearer <user token>) {question, transfer_id?}
      → InvestigateTransferResponse with Cache-Control: no-store. Another sender's transfer and a missing one
      → the identical 404.
    - POST /v1/assistant/investigate-fraud (merchant token, same body as /ask) → InvestigateFraudResponse with
      Cache-Control: no-store; now = deps.clock().
    Missing/invalid/expired token → 401 {"detail": "invalid token"}. A valid token of the other kind
    (user token on the merchant routes, merchant token on the transfer route) → 403 {"detail": "forbidden"}.
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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        failed = deps.readiness()
        if failed:
            return JSONResponse({"status": "not_ready", "failed": failed}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/assistant/ask")
    def assistant_ask(body: AskRequest, scope: Annotated[Scope, Depends(merchant_scope)]) -> AskResponse:
        with deps.session_factory() as session:
            answer = ask(session, scope, body.question, deps.llm)
        return AskResponse(
            mode=answer.mode,
            text=answer.text,
            model=answer.model,
            evidence=[EvidenceOut(ref=item.ref, summary=item.summary) for item in answer.evidence],
        )

    @app.post("/v1/assistant/investigate")
    def assistant_investigate(
        body: AskRequest, scope: Annotated[Scope, Depends(merchant_scope)]
    ) -> InvestigateResponse:
        with deps.session_factory() as session:
            result = investigate(session, scope, body.question, deps.clock(), deps.llm)
        return InvestigateResponse(
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

    return app


async def _validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's default 422 echoes the rejected input back."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in errors]
    return JSONResponse({"error": "validation_failed", "detail": detail}, status_code=422)
