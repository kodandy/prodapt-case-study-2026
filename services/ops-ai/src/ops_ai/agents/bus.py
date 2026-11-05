"""In-process message bus with a hard message budget, an optional Redis-stream mirror, and the audit trail."""

import uuid
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Protocol

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.agents.messages import (
    AgentMessage,
    AgentName,
    JsonScalar,
    MessageIntent,
    agent_name,
    evidence_ref_tuple,
    json_payload,
    message_intent,
    message_to_json,
)
from ops_ai.agents.models import AgentMessageRecord
from ops_ai.clock import Clock
from ops_ai.scope import Scope

MAX_MESSAGES = 16
DEFAULT_STREAM = "ops_ai:agent_messages"
DEFAULT_STREAM_MAXLEN = 10_000


class MessageTransport(Protocol):
    def publish(self, message: AgentMessage) -> None: ...


class RedisStreamTransport:
    """Mirrors every message to a capped Redis stream so other processes can follow an investigation."""

    def __init__(
        self, redis: Redis, stream: str = DEFAULT_STREAM, maxlen: int = DEFAULT_STREAM_MAXLEN
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    def publish(self, message: AgentMessage) -> None:
        """Best effort: the persisted trail is the audit record, so a Redis outage never fails the
        investigation (and the payload is never logged)."""
        try:
            self._redis.xadd(
                self._stream, {"message": message_to_json(message)}, maxlen=self._maxlen, approximate=True
            )
        except RedisError:
            return


class MessageBudgetExceededError(Exception):
    pass


class InProcessBus:
    """FIFO delivery of one investigation's messages. Delivery never frees budget: the budget bounds the
    whole conversation, not the queue."""

    def __init__(
        self,
        correlation_id: uuid.UUID,
        clock: Clock,
        max_messages: int = MAX_MESSAGES,
        transport: MessageTransport | None = None,
    ) -> None:
        self.correlation_id = correlation_id
        self._clock = clock
        self._max_messages = max_messages
        self._transport = transport
        self._queue: deque[AgentMessage] = deque()
        self._history: list[AgentMessage] = []

    def send(
        self,
        sender: AgentName,
        recipient: AgentName,
        intent: MessageIntent,
        payload: Mapping[str, JsonScalar],
        evidence_refs: Sequence[str] = (),
    ) -> AgentMessage:
        if len(self._history) >= self._max_messages:
            raise MessageBudgetExceededError(f"more than {self._max_messages} messages")
        message = AgentMessage(
            message_id=uuid.uuid4(),
            correlation_id=self.correlation_id,
            sequence=len(self._history) + 1,
            sender=sender,
            recipient=recipient,
            intent=intent,
            payload=dict(payload),
            evidence_refs=tuple(evidence_refs),
            created_at=self._clock(),
        )
        self._history.append(message)
        self._queue.append(message)
        if self._transport is not None:
            self._transport.publish(message)
        return message

    def next_message(self) -> AgentMessage | None:
        return self._queue.popleft() if self._queue else None

    @property
    def history(self) -> tuple[AgentMessage, ...]:
        return tuple(self._history)


def persist_messages(
    session: Session,
    messages: Sequence[AgentMessage],
    merchant_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
) -> int:
    """Adds the audit rows and flushes; the caller commits."""
    session.add_all(
        [
            AgentMessageRecord(
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                sequence=message.sequence,
                sender=message.sender,
                recipient=message.recipient,
                intent=message.intent,
                payload=dict(message.payload),
                evidence_refs=list(message.evidence_refs),
                merchant_id=merchant_id,
                user_id=user_id,
                created_at=message.created_at,
            )
            for message in messages
        ]
    )
    session.flush()
    return len(messages)


def load_messages(session: Session, scope: Scope, correlation_id: uuid.UUID) -> list[AgentMessage]:
    """The scope merchant's trail only; a user-scoped trail has no merchant and is never returned."""
    rows = session.scalars(
        select(AgentMessageRecord)
        .where(
            AgentMessageRecord.correlation_id == correlation_id,
            AgentMessageRecord.merchant_id == scope.merchant_id,
        )
        .order_by(AgentMessageRecord.sequence)
    )
    return [
        AgentMessage(
            message_id=row.message_id,
            correlation_id=row.correlation_id,
            sequence=row.sequence,
            sender=agent_name(row.sender),
            recipient=agent_name(row.recipient),
            intent=message_intent(row.intent),
            payload=json_payload(row.payload),
            evidence_refs=evidence_ref_tuple(row.evidence_refs),
            created_at=row.created_at,
        )
        for row in rows
    ]
