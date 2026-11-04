"""HTTP for B10 (docs/specs/memory.md). The merchant always comes from the `merchant_scope` dependency."""

import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.assistant.llm_port import LlmPort
from ops_ai.assistant.service import AnswerMode
from ops_ai.clock import Clock
from ops_ai.memory.conversation import confirm_memory, take_turn
from ops_ai.memory.session import ChatSession, SessionStore, owner_for
from ops_ai.memory.state import MAX_MEMORY_CHARS, ChatIntent, Metric, TimeWindow, is_storable_memory
from ops_ai.memory.store import MemoryRecord, correct_memory, delete_memory, recall_memories
from ops_ai.scope import Scope

SESSION_NOT_FOUND = "session not found"
PROPOSAL_NOT_FOUND = "proposal not found"
MEMORY_NOT_FOUND = "memory not found"
NO_STORE = {"Cache-Control": "no-store"}


class TurnRequest(BaseModel):
    """No tenant field: the scope comes from the token only, so `merchant_id` here is a 422."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=2000)


class CorrectMemoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    content: str = Field(min_length=1, max_length=MAX_MEMORY_CHARS)

    @field_validator("content")
    @classmethod
    def _storable(cls, value: str) -> str:
        if not is_storable_memory(value):
            raise ValueError("content must not contain hidden characters or card numbers")
        return value


class SessionCreatedOut(BaseModel):
    session_id: uuid.UUID
    expires_in_seconds: int


class WindowOut(BaseModel):
    start: datetime
    end: datetime

    @classmethod
    def of(cls, window: TimeWindow | None) -> "WindowOut | None":
        return None if window is None else cls(start=window.start, end=window.end)


class StateOut(BaseModel):
    metric: Metric | None
    window: WindowOut | None


class TurnSummaryOut(BaseModel):
    turn_id: uuid.UUID
    question: str
    intent: ChatIntent
    metric: Metric | None
    window: WindowOut | None


class SessionOut(BaseModel):
    session_id: uuid.UUID
    state: StateOut
    turns: list[TurnSummaryOut]


class MetricResultOut(BaseModel):
    total: int
    failed: int
    rate: float


class ToolCallOut(BaseModel):
    name: str
    args: dict[str, str]


class EvidenceOut(BaseModel):
    ref: str
    summary: str


class RecalledMemoryOut(BaseModel):
    memory_id: uuid.UUID
    content: str
    source_turn_id: uuid.UUID


class PendingMemoryOut(BaseModel):
    proposal_id: uuid.UUID
    content: str


class TurnOut(BaseModel):
    session_id: uuid.UUID
    turn_id: uuid.UUID
    intent: ChatIntent
    metric: Metric | None
    window: WindowOut | None
    mode: AnswerMode
    text: str | None
    model: str | None
    result: MetricResultOut | None
    tool_calls: list[ToolCallOut]
    evidence: list[EvidenceOut]
    memories: list[RecalledMemoryOut]
    pending_memory: PendingMemoryOut | None


class MemoryOut(BaseModel):
    memory_id: uuid.UUID
    content: str
    source_session_id: uuid.UUID
    source_turn_id: uuid.UUID
    supersedes_id: uuid.UUID | None
    created_at: datetime

    @classmethod
    def of(cls, record: MemoryRecord) -> "MemoryOut":
        return cls(
            memory_id=record.memory_id,
            content=record.content,
            source_session_id=record.source_session_id,
            source_turn_id=record.source_turn_id,
            supersedes_id=record.supersedes_id,
            created_at=record.created_at,
        )


class MemoryListOut(BaseModel):
    memories: list[MemoryOut]


def build_memory_router(
    *,
    session_factory: sessionmaker[Session],
    store: SessionStore,
    llm: LlmPort,
    clock: Clock,
    merchant_scope: Callable[..., Scope],
) -> APIRouter:
    router = APIRouter(prefix="/v1/assistant")
    MerchantScope = Annotated[Scope, Depends(merchant_scope)]

    def owned_session(scope: Scope, session_id: uuid.UUID) -> ChatSession:
        chat = store.load(owner_for(scope), session_id)
        if chat is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, SESSION_NOT_FOUND, headers=NO_STORE)
        return chat

    @router.post("/sessions", status_code=status.HTTP_201_CREATED)
    def create_session(scope: MerchantScope, response: Response) -> SessionCreatedOut:
        chat = store.create(owner_for(scope), clock())
        response.headers["Cache-Control"] = "no-store"
        return SessionCreatedOut(session_id=chat.session_id, expires_in_seconds=store.ttl_seconds)

    @router.get("/sessions/{session_id}")
    def get_session(session_id: uuid.UUID, scope: MerchantScope, response: Response) -> SessionOut:
        chat = owned_session(scope, session_id)
        response.headers["Cache-Control"] = "no-store"
        return SessionOut(
            session_id=chat.session_id,
            state=StateOut(metric=chat.state.metric, window=WindowOut.of(chat.state.window)),
            turns=[
                TurnSummaryOut(
                    turn_id=t.turn_id,
                    question=t.question,
                    intent=t.intent,
                    metric=t.metric,
                    window=WindowOut.of(t.window),
                )
                for t in chat.turns
            ],
        )

    @router.post("/sessions/{session_id}/turns")
    def post_turn(
        session_id: uuid.UUID, body: TurnRequest, scope: MerchantScope, response: Response
    ) -> TurnOut:
        chat = owned_session(scope, session_id)
        with session_factory() as db:
            result = take_turn(db, store, chat, scope, body.question, clock(), llm)
        response.headers["Cache-Control"] = "no-store"
        aggregate = result.aggregate
        return TurnOut(
            session_id=result.session.session_id,
            turn_id=result.turn.turn_id,
            intent=result.turn.intent,
            metric=result.turn.metric,
            window=WindowOut.of(result.turn.window),
            mode=result.mode,
            text=result.text,
            model=result.model,
            result=None
            if aggregate is None
            else MetricResultOut(total=aggregate.total, failed=aggregate.failed, rate=aggregate.rate),
            tool_calls=[ToolCallOut(name=c.name, args=c.args) for c in result.tool_calls],
            evidence=[EvidenceOut(ref=e.ref, summary=e.summary) for e in result.evidence],
            memories=[
                RecalledMemoryOut(memory_id=m.memory_id, content=m.content, source_turn_id=m.source_turn_id)
                for m in result.memories
            ],
            pending_memory=None
            if result.pending is None
            else PendingMemoryOut(proposal_id=result.pending.proposal_id, content=result.pending.content),
        )

    @router.post("/sessions/{session_id}/memory-proposals/{proposal_id}/confirm", status_code=201)
    def confirm(
        session_id: uuid.UUID, proposal_id: uuid.UUID, scope: MerchantScope, response: Response
    ) -> MemoryOut:
        chat = owned_session(scope, session_id)
        with session_factory() as db:
            record = confirm_memory(db, store, chat, scope, proposal_id, clock())
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, PROPOSAL_NOT_FOUND, headers=NO_STORE)
        response.headers["Cache-Control"] = "no-store"
        return MemoryOut.of(record)

    @router.get("/memories")
    def list_memories(scope: MerchantScope, response: Response) -> MemoryListOut:
        with session_factory() as db:
            records = recall_memories(db, scope)
        response.headers["Cache-Control"] = "no-store"
        return MemoryListOut(memories=[MemoryOut.of(r) for r in records])

    @router.patch("/memories/{memory_id}")
    def patch_memory(
        memory_id: uuid.UUID, body: CorrectMemoryRequest, scope: MerchantScope, response: Response
    ) -> MemoryOut:
        with session_factory() as db:
            record = correct_memory(db, scope, owner_for(scope), memory_id, body.content, clock())
        if record is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, MEMORY_NOT_FOUND, headers=NO_STORE)
        response.headers["Cache-Control"] = "no-store"
        return MemoryOut.of(record)

    @router.delete("/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
    def remove_memory(memory_id: uuid.UUID, scope: MerchantScope) -> Response:
        with session_factory() as db:
            deleted = delete_memory(db, scope, owner_for(scope), memory_id, clock())
        if not deleted:
            raise HTTPException(status.HTTP_404_NOT_FOUND, MEMORY_NOT_FOUND, headers=NO_STORE)
        return Response(status_code=status.HTTP_204_NO_CONTENT, headers={"Cache-Control": "no-store"})

    return router
