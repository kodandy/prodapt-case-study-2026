"""Redis chat sessions (B10.1), keyed by (merchant_id, user_id, session_id) from auth, with a TTL."""

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from typing import cast, get_args

from redis import Redis

from ops_ai.memory.state import EMPTY_STATE, ChatIntent, ConversationState, Metric, TimeWindow
from ops_ai.scope import Scope

SESSION_TTL_SECONDS = 1800
MAX_TURNS = 10
MAX_PENDING = 5
KEY_PREFIX = "ops_ai:session"


@dataclass(frozen=True)
class SessionOwner:
    merchant_id: uuid.UUID
    user_id: str


def owner_for(scope: Scope) -> SessionOwner:
    """Merchant tokens carry no operator id (`sub` is `merchant:<id>`): the user slot holds the subject."""
    return SessionOwner(merchant_id=scope.merchant_id, user_id=f"merchant:{scope.merchant_id}")


@dataclass(frozen=True)
class Turn:
    turn_id: uuid.UUID
    question: str
    intent: ChatIntent
    metric: Metric | None
    window: TimeWindow | None
    answer: str | None
    at: datetime


@dataclass(frozen=True)
class PendingMemory:
    proposal_id: uuid.UUID
    content: str
    source_turn_id: uuid.UUID


@dataclass(frozen=True)
class ChatSession:
    session_id: uuid.UUID
    owner: SessionOwner
    created_at: datetime
    turns: tuple[Turn, ...]
    state: ConversationState
    pending: tuple[PendingMemory, ...]


def session_key(owner: SessionOwner, session_id: uuid.UUID) -> str:
    return f"{KEY_PREFIX}:{owner.merchant_id}:{owner.user_id}:{session_id}"


class SessionStore:
    def __init__(self, redis: Redis, ttl_seconds: int = SESSION_TTL_SECONDS) -> None:
        self._redis = redis
        self.ttl_seconds = ttl_seconds

    def create(self, owner: SessionOwner, now: datetime) -> ChatSession:
        chat = ChatSession(
            session_id=uuid.uuid4(), owner=owner, created_at=now, turns=(), state=EMPTY_STATE, pending=()
        )
        self.save(chat)
        return chat

    def load(self, owner: SessionOwner, session_id: uuid.UUID) -> ChatSession | None:
        """None when missing, expired, unparseable, or stored for another owner/session (ownership check)."""
        raw = self._redis.get(session_key(owner, session_id))
        if not isinstance(raw, (str, bytes)):
            return None
        try:
            chat = _decode(json.loads(raw))
        except (ValueError, KeyError, TypeError):
            return None
        if chat.owner != owner or chat.session_id != session_id:
            return None
        return chat

    def save(self, chat: ChatSession) -> None:
        trimmed = replace(chat, turns=chat.turns[-MAX_TURNS:], pending=chat.pending[-MAX_PENDING:])
        self._redis.set(
            session_key(chat.owner, chat.session_id), json.dumps(_encode(trimmed)), ex=self.ttl_seconds
        )


def _window_out(window: TimeWindow | None) -> dict[str, str] | None:
    if window is None:
        return None
    return {"start": window.start.isoformat(), "end": window.end.isoformat()}


def _window_in(value: object) -> TimeWindow | None:
    if value is None:
        return None
    data = cast(dict[str, str], value)
    return TimeWindow(start=datetime.fromisoformat(data["start"]), end=datetime.fromisoformat(data["end"]))


def _metric_in(value: object) -> Metric | None:
    if value is None:
        return None
    if value not in get_args(Metric):
        raise ValueError("unknown metric")
    return cast(Metric, value)


def _intent_in(value: object) -> ChatIntent:
    if value not in get_args(ChatIntent):
        raise ValueError("unknown intent")
    return cast(ChatIntent, value)


def _encode(chat: ChatSession) -> dict[str, object]:
    return {
        "session_id": str(chat.session_id),
        "merchant_id": str(chat.owner.merchant_id),
        "user_id": chat.owner.user_id,
        "created_at": chat.created_at.isoformat(),
        "state": {"metric": chat.state.metric, "window": _window_out(chat.state.window)},
        "turns": [
            {
                "turn_id": str(t.turn_id),
                "question": t.question,
                "intent": t.intent,
                "metric": t.metric,
                "window": _window_out(t.window),
                "answer": t.answer,
                "at": t.at.isoformat(),
            }
            for t in chat.turns
        ],
        "pending": [
            {"proposal_id": str(p.proposal_id), "content": p.content, "source_turn_id": str(p.source_turn_id)}
            for p in chat.pending
        ],
    }


def _decode(data: dict[str, object]) -> ChatSession:
    state = cast(dict[str, object], data["state"])
    turns = cast(list[dict[str, object]], data["turns"])
    pending = cast(list[dict[str, object]], data["pending"])
    return ChatSession(
        session_id=uuid.UUID(str(data["session_id"])),
        owner=SessionOwner(merchant_id=uuid.UUID(str(data["merchant_id"])), user_id=str(data["user_id"])),
        created_at=datetime.fromisoformat(str(data["created_at"])),
        state=ConversationState(metric=_metric_in(state["metric"]), window=_window_in(state["window"])),
        turns=tuple(
            Turn(
                turn_id=uuid.UUID(str(t["turn_id"])),
                question=str(t["question"]),
                intent=_intent_in(t["intent"]),
                metric=_metric_in(t["metric"]),
                window=_window_in(t["window"]),
                answer=None if t["answer"] is None else str(t["answer"]),
                at=datetime.fromisoformat(str(t["at"])),
            )
            for t in turns
        ),
        pending=tuple(
            PendingMemory(
                proposal_id=uuid.UUID(str(p["proposal_id"])),
                content=str(p["content"]),
                source_turn_id=uuid.UUID(str(p["source_turn_id"])),
            )
            for p in pending
        ),
    )
