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
    message_from_json,
    message_intent,
    message_to_json,
)
from ops_ai.agents.models import AgentMessageRecord
from ops_ai.clock import Clock
from ops_ai.scope import Scope, UserScope

MAX_MESSAGES = 16
DEFAULT_STREAM = "ops_ai:agent_messages"
DEFAULT_STREAM_MAXLEN = 10_000


TENANT_STREAM_PREFIX = "ops_ai:agent_messages"
TENANT_STREAM_MAXLEN = 1_000
DEFAULT_TENANT_READ_COUNT = 100

TenantPrincipal = Scope | UserScope


class MessageTransport(Protocol):
    def publish(self, message: AgentMessage) -> None: ...


class TenantTransportFactory(Protocol):
    def for_principal(self, principal: TenantPrincipal) -> MessageTransport: ...


def tenant_stream_key(principal: TenantPrincipal) -> str:
    """Derived from the principal only: the per-tenant key is the Redis ACL unit."""
    if isinstance(principal, Scope):
        return f"{TENANT_STREAM_PREFIX}:merchant:{principal.merchant_id}"
    return f"{TENANT_STREAM_PREFIX}:user:{principal.user_id}"


def _xadd_best_effort(
    redis: Redis, stream: str, message: AgentMessage, maxlen: int, approximate: bool
) -> None:
    """The persisted trail is the audit record, so a Redis outage never fails the investigation (and the
    payload is never logged)."""
    try:
        redis.xadd(stream, {"message": message_to_json(message)}, maxlen=maxlen, approximate=approximate)
    except RedisError:
        return


class RedisStreamTransport:
    """Mirrors every message to a capped, ops-wide Redis stream; `for_principal` adds the tenant's own
    stream."""

    def __init__(
        self,
        redis: Redis,
        stream: str = DEFAULT_STREAM,
        maxlen: int = DEFAULT_STREAM_MAXLEN,
        tenant_maxlen: int = TENANT_STREAM_MAXLEN,
    ) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen
        self._tenant_maxlen = tenant_maxlen

    def publish(self, message: AgentMessage) -> None:
        _xadd_best_effort(self._redis, self._stream, message, self._maxlen, approximate=True)

    def for_principal(self, principal: TenantPrincipal) -> "TenantStreamTransport":
        return TenantStreamTransport(self, self._redis, tenant_stream_key(principal), self._tenant_maxlen)


class TenantStreamTransport:
    """The shared mirror plus one tenant's stream, trimmed exactly so it never exceeds its bound."""

    def __init__(self, shared: RedisStreamTransport, redis: Redis, stream_key: str, maxlen: int) -> None:
        self._shared = shared
        self._redis = redis
        self._maxlen = maxlen
        self.stream_key = stream_key

    def publish(self, message: AgentMessage) -> None:
        self._shared.publish(message)
        _xadd_best_effort(self._redis, self.stream_key, message, self._maxlen, approximate=False)


def read_tenant_messages(
    redis: Redis, principal: TenantPrincipal, count: int = DEFAULT_TENANT_READ_COUNT
) -> list[AgentMessage]:
    """The newest `count` messages of the principal's own stream, oldest first; no caller-chosen key."""
    if count <= 0:
        return []
    try:
        raw = redis.xrevrange(tenant_stream_key(principal), count=count)
    except RedisError:
        return []
    if not raw:
        return []
    messages: list[AgentMessage] = []
    for _entry_id, fields in reversed(raw):
        parsed = _parse_entry(fields)
        if parsed is not None:
            messages.append(parsed)
    return messages


def _text(value: object) -> str | None:
    """Clients with and without `decode_responses` both read back."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else None


def _parse_entry(fields: object) -> AgentMessage | None:
    if not isinstance(fields, dict):
        return None
    raw = next((_text(value) for key, value in fields.items() if _text(key) == "message"), None)
    if raw is None:
        return None
    try:
        return message_from_json(raw)
    except (ValueError, KeyError, TypeError):
        return None


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
