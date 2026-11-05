import uuid
from collections.abc import Callable, Sequence
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.agents.bus import MessageTransport, load_messages
from ops_ai.agents.messages import AgentContribution, AgentMessage, JsonScalar
from ops_ai.agents.rca import narrate_transfer_rca, run_transfer_rca
from ops_ai.agents.supervisor import narrate_investigation, run_agents
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort
from ops_ai.clock import Clock
from ops_ai.judge.dispute_summary import DisputeRecordSource
from ops_ai.scope import Scope, UserScope

NO_STORE = "no-store"


class AgentInvestigateRequest(BaseModel):
    """No tenant field: the merchant comes from the token only."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[StrictStr, Field(min_length=1, max_length=2000)]


class TransferRcaRequest(BaseModel):
    """No sender field: the sender comes from the token only."""

    model_config = ConfigDict(extra="forbid")

    question: Annotated[StrictStr, Field(min_length=1, max_length=2000)]
    transfer_id: uuid.UUID | None = None


class EvidenceOut(BaseModel):
    ref: str
    summary: str


class ToolCallOut(BaseModel):
    name: str
    args: dict[str, str]


class ContributionOut(BaseModel):
    agent: str
    status: str
    summary: str
    evidence: list[EvidenceOut]
    tool_calls: list[ToolCallOut]


class MessageOut(BaseModel):
    sequence: int
    sender: str
    recipient: str
    intent: str
    payload: dict[str, JsonScalar]
    evidence_refs: list[str]


class AgentInvestigateResponse(BaseModel):
    correlation_id: uuid.UUID
    plan: list[str]
    finding: str
    contributions: list[ContributionOut]
    messages: list[MessageOut]
    evidence: list[EvidenceOut]
    truncated: bool
    mode: Literal["llm", "t2_fallback"]
    text: str | None
    model: str | None


class TransferRcaResponse(BaseModel):
    correlation_id: uuid.UUID
    cause_code: str
    remediation_owner: str
    contributions: list[ContributionOut]
    messages: list[MessageOut]
    suggested_fixes: list[str]
    evidence: list[EvidenceOut]
    truncated: bool
    mode: Literal["llm", "t2_fallback"]
    text: str | None
    model: str | None


class MessagesResponse(BaseModel):
    messages: list[MessageOut]


def _evidence(items: Sequence[EvidenceItem]) -> list[EvidenceOut]:
    return [EvidenceOut(ref=item.ref, summary=item.summary) for item in items]


def _contributions(contributions: Sequence[AgentContribution]) -> list[ContributionOut]:
    return [
        ContributionOut(
            agent=contribution.agent,
            status=contribution.status,
            summary=contribution.summary,
            evidence=_evidence(contribution.evidence),
            tool_calls=[ToolCallOut(name=call.name, args=call.args) for call in contribution.tool_calls],
        )
        for contribution in contributions
    ]


def _messages(messages: Sequence[AgentMessage]) -> list[MessageOut]:
    return [
        MessageOut(
            sequence=message.sequence,
            sender=message.sender,
            recipient=message.recipient,
            intent=message.intent,
            payload=dict(message.payload),
            evidence_refs=list(message.evidence_refs),
        )
        for message in messages
    ]


def create_agents_router(
    session_factory: sessionmaker[Session],
    llm: LlmPort,
    clock: Clock,
    merchant_scope: Callable[..., Scope],
    user_scope: Callable[..., UserScope],
    transport: MessageTransport | None = None,
    *,
    dispute_records: DisputeRecordSource | None = None,
) -> APIRouter:
    """POST /v1/agents/investigate and GET /v1/agents/messages/{id} (merchant token), POST
    /v1/agents/transfer-rca (user token)."""
    router = APIRouter()

    @router.post("/v1/agents/investigate")
    def post_agents_investigate(
        body: AgentInvestigateRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> AgentInvestigateResponse:
        with session_factory() as session:
            found = run_agents(session, scope, body.question, clock(), transport, dispute_records)
            session.commit()
        result = narrate_investigation(found, body.question, llm)
        response.headers["Cache-Control"] = NO_STORE
        return AgentInvestigateResponse(
            correlation_id=result.correlation_id,
            plan=list(result.plan),
            finding=result.finding,
            contributions=_contributions(result.contributions),
            messages=_messages(result.messages),
            evidence=_evidence(result.evidence),
            truncated=result.truncated,
            mode=result.mode,
            text=result.text,
            model=result.model,
        )

    @router.get("/v1/agents/messages/{correlation_id}")
    def get_agent_messages(
        correlation_id: uuid.UUID, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> MessagesResponse:
        with session_factory() as session:
            messages = load_messages(session, scope, correlation_id)
        if not messages:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
        response.headers["Cache-Control"] = NO_STORE
        return MessagesResponse(messages=_messages(messages))

    @router.post("/v1/agents/transfer-rca")
    def post_transfer_rca(
        body: TransferRcaRequest, scope: Annotated[UserScope, Depends(user_scope)], response: Response
    ) -> TransferRcaResponse:
        with session_factory() as session:
            found = run_transfer_rca(session, scope, body.transfer_id, clock(), transport)
            session.commit()
        if found is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "transfer not found")
        result = narrate_transfer_rca(found, body.question, llm)
        response.headers["Cache-Control"] = NO_STORE
        return TransferRcaResponse(
            correlation_id=result.correlation_id,
            cause_code=result.cause_code,
            remediation_owner=result.remediation_owner,
            contributions=_contributions(result.contributions),
            messages=_messages(result.messages),
            suggested_fixes=list(result.suggested_fixes),
            evidence=_evidence(result.evidence),
            truncated=result.truncated,
            mode=result.mode,
            text=result.text,
            model=result.model,
        )

    return router
