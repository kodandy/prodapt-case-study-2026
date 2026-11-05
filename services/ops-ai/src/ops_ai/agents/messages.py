"""The typed envelope agents exchange (R2-6) and the contribution each agent reports (R2-1)."""

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast, get_args

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.investigation.service import ToolCall

AgentName = Literal["supervisor", "fraud", "settlement", "dispute", "compliance"]
MessageIntent = Literal[
    "investigate",
    "assess_transfer",
    "check_kyc",
    "verify_funds",
    "funds_verified",
    "check_disputes",
    "chargeback_impact",
    "chargeback_impact_result",
    "finding",
    "root_cause",
]
JsonScalar = str | int | float | bool | None
ContributionStatus = Literal["finding", "no_finding"]

_AGENT_NAMES: frozenset[str] = frozenset(get_args(AgentName))
_INTENTS: frozenset[str] = frozenset(get_args(MessageIntent))


@dataclass(frozen=True)
class AgentMessage:
    message_id: uuid.UUID
    correlation_id: uuid.UUID
    sequence: int
    sender: AgentName
    recipient: AgentName
    intent: MessageIntent
    payload: Mapping[str, JsonScalar]
    evidence_refs: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True)
class AgentContribution:
    """What one agent found while handling its messages, with the tools it actually called."""

    agent: AgentName
    status: ContributionStatus
    summary: str
    evidence: tuple[EvidenceItem, ...]
    tool_calls: tuple[ToolCall, ...]


def message_to_json(message: AgentMessage) -> str:
    return json.dumps(
        {
            "message_id": str(message.message_id),
            "correlation_id": str(message.correlation_id),
            "sequence": message.sequence,
            "sender": message.sender,
            "recipient": message.recipient,
            "intent": message.intent,
            "payload": dict(message.payload),
            "evidence_refs": list(message.evidence_refs),
            "created_at": message.created_at.isoformat(),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def agent_name(value: object) -> AgentName:
    if not isinstance(value, str) or value not in _AGENT_NAMES:
        raise ValueError("unknown agent")
    return cast(AgentName, value)


def message_intent(value: object) -> MessageIntent:
    if not isinstance(value, str) or value not in _INTENTS:
        raise ValueError("unknown intent")
    return cast(MessageIntent, value)


def json_payload(value: object) -> dict[str, JsonScalar]:
    if not isinstance(value, dict):
        raise ValueError("payload must be an object")
    payload: dict[str, JsonScalar] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not (item is None or isinstance(item, str | int | float | bool)):
            raise ValueError("payload values must be JSON scalars")
        payload[key] = item
    return payload


def evidence_ref_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("evidence_refs must be a list of strings")
    return tuple(str(item) for item in value)


def message_from_json(raw: str) -> AgentMessage:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("message must be an object")
    sequence = data["sequence"]
    created_at = datetime.fromisoformat(str(data["created_at"]))
    if not isinstance(sequence, int) or isinstance(sequence, bool) or created_at.tzinfo is None:
        raise ValueError("invalid message")
    return AgentMessage(
        message_id=uuid.UUID(str(data["message_id"])),
        correlation_id=uuid.UUID(str(data["correlation_id"])),
        sequence=sequence,
        sender=agent_name(data["sender"]),
        recipient=agent_name(data["recipient"]),
        intent=message_intent(data["intent"]),
        payload=json_payload(data["payload"]),
        evidence_refs=evidence_ref_tuple(data["evidence_refs"]),
        created_at=created_at,
    )
