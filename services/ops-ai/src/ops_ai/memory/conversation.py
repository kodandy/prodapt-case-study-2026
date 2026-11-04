"""One chat turn over a session (B10.2) and the confirmed write of a long-term memory (B10.3)."""

import uuid
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.investigation.service import ToolCall
from ops_ai.investigation.tools import FailureAggregate, aggregate_failures
from ops_ai.memory.session import ChatSession, PendingMemory, SessionStore, Turn, owner_for
from ops_ai.memory.state import Metric, TimeWindow, detect_memory_request, resolve
from ops_ai.memory.store import DuplicateMemoryError, MemoryRecord, recall_memories, save_confirmed_memory
from ops_ai.scope import Scope

MEMORY_LABEL = "Merchant-stated note (unverified, not a source for numbers): "


@dataclass(frozen=True)
class TurnResult:
    session: ChatSession
    turn: Turn
    aggregate: FailureAggregate | None
    tool_calls: tuple[ToolCall, ...]
    evidence: tuple[EvidenceItem, ...]
    memories: list[MemoryRecord]
    pending: PendingMemory | None
    mode: AnswerMode
    text: str | None
    model: str | None


def _owned(chat: ChatSession, scope: Scope) -> None:
    if chat.owner != owner_for(scope):
        raise PermissionError("session does not belong to this scope")


def _metric_evidence(metric: Metric, window: TimeWindow, aggregate: FailureAggregate) -> EvidenceItem:
    span = f"{window.start.isoformat()} to {window.end.isoformat()}"
    if metric == "payment_volume":
        return EvidenceItem(ref="aggregate:window", summary=f"{aggregate.total} card payments, {span}")
    rate = (aggregate.failed / aggregate.total if aggregate.total else 0.0) * 100
    return EvidenceItem(
        ref="aggregate:window",
        summary=f"{aggregate.failed} of {aggregate.total} card payments failed ({rate:.1f}%), {span}",
    )


def take_turn(
    db: Session,
    store: SessionStore,
    chat: ChatSession,
    scope: Scope,
    question: str,
    now: datetime,
    llm: LlmPort,
) -> TurnResult:
    """Merchant = `scope`; metric and window come from the question or the session state, never from the LLM.

    A memory proposal only ever comes from the user's own question and is never written here.
    """
    _owned(chat, scope)
    bounded = question[:MAX_QUESTION_CHARS]
    intent, state = resolve(bounded, chat.state, now)
    turn_id = uuid.uuid4()
    memories = recall_memories(db, scope)
    memory_evidence = tuple(
        EvidenceItem(ref=f"memory:{m.memory_id}", summary=f"{MEMORY_LABEL}{m.content}") for m in memories
    )
    aggregate: FailureAggregate | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    pending: PendingMemory | None = None
    if intent == "metric_query" and state.metric is not None and state.window is not None:
        window = state.window
        aggregate = aggregate_failures(db, scope, window.start, window.end)
        tool_calls = (
            ToolCall(
                "aggregate_failures",
                {"start": window.start.isoformat(), "end": window.end.isoformat(), "metric": state.metric},
            ),
        )
        evidence = (_metric_evidence(state.metric, window, aggregate), *memory_evidence)
    elif intent == "memory_proposal":
        content = detect_memory_request(bounded)
        if content is not None:
            pending = PendingMemory(proposal_id=uuid.uuid4(), content=content, source_turn_id=turn_id)
            tool_calls = (ToolCall("propose_merchant_memory", {"content": content}),)
    db.close()
    mode: AnswerMode = "t2_fallback"
    text: str | None = None
    model: str | None = None
    if intent == "metric_query":
        try:
            response = llm.complete(LlmRequest(question=bounded, evidence=evidence))
            mode, text, model = "llm", response.text, response.model
        except LlmUnavailableError:
            pass
    turn = Turn(
        turn_id=turn_id,
        question=bounded,
        intent=intent,
        metric=state.metric if intent == "metric_query" else None,
        window=state.window if intent == "metric_query" else None,
        answer=text,
        at=now,
    )
    updated = replace(
        chat,
        turns=(*chat.turns, turn),
        state=state if intent == "metric_query" else chat.state,
        pending=(*chat.pending, pending) if pending is not None else chat.pending,
    )
    store.save(updated)
    return TurnResult(
        session=updated,
        turn=turn,
        aggregate=aggregate,
        tool_calls=tool_calls,
        evidence=evidence,
        memories=memories,
        pending=pending,
        mode=mode,
        text=text,
        model=model,
    )


def confirm_memory(
    db: Session, store: SessionStore, chat: ChatSession, scope: Scope, proposal_id: uuid.UUID, now: datetime
) -> MemoryRecord | None:
    """The only writer of new long-term memories: an explicit user confirmation of a pending proposal.

    The insert is idempotent per source turn, so a proposal revived by a concurrent turn's save (or confirmed
    twice at once) writes at most one row, and never resurrects a deleted memory: the retry returns None.
    """
    _owned(chat, scope)
    proposal = next((p for p in chat.pending if p.proposal_id == proposal_id), None)
    if proposal is None:
        return None
    try:
        record: MemoryRecord | None = save_confirmed_memory(
            db, scope, chat.owner, chat.session_id, proposal, now
        )
    except DuplicateMemoryError:
        record = None
    store.save(replace(chat, pending=tuple(p for p in chat.pending if p.proposal_id != proposal_id)))
    return record
