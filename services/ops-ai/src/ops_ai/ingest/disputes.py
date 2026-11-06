"""Consumes gw.disputes.v1 into ops_ai.dispute_workflows / dispute_workflow_steps (R2-1 / R2-2): the dispute
workflow records the dispute judge rates (docs/specs/evaluation-judge.md 6.4.1)."""

import hashlib
import json
import logging

from contracts.events import DISPUTES_TOPIC, DisputeEventV1
from redis import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.ingest.consumer import DEAD_LETTER_MAXLEN, ensure_group
from ops_ai.ingest.models import DisputeWorkflow, DisputeWorkflowStep, ProcessedEvent

__all__ = [
    "DISPUTES_DEAD_LETTER_TOPIC",
    "DISPUTE_WORKFLOWS_GROUP",
    "apply_dispute_event",
    "consume_disputes",
]

logger = logging.getLogger(__name__)

DISPUTE_WORKFLOWS_GROUP = "ops-ai.dispute-workflows"
DISPUTES_DEAD_LETTER_TOPIC = "gw.disputes.v1.dead"


def apply_dispute_event(session: Session, clock: Clock, event: DisputeEventV1) -> bool:
    """Upserts the dispute snapshot (highest entity_version wins) and records the transition step.

    Dedupes on processed_events(DISPUTE_WORKFLOWS_GROUP, event_id) in the same transaction; False for a
    duplicate. A dispute's merchant is immutable once projected: the upsert only updates a row of the same
    merchant, and an event for another merchant is consumed, dropped and logged by id. Does not commit.
    """
    first_delivery = session.execute(
        insert(ProcessedEvent)
        .values(consumer=DISPUTE_WORKFLOWS_GROUP, event_id=event.event_id, processed_at=clock())
        .on_conflict_do_nothing()
        .returning(ProcessedEvent.event_id)
    ).scalar_one_or_none()
    if first_delivery is None:
        return False
    snapshot = {
        "dispute_id": event.dispute_id,
        "merchant_id": event.merchant_id,
        "payment_id": event.payment_id,
        "network_reason_code": event.network_reason_code,
        "currency": event.currency,
        "payment_amount_minor": event.payment_amount_minor,
        "refunded_minor": event.refunded_minor,
        "dispute_amount_minor": event.dispute_amount_minor,
        "chargeback_minor": event.chargeback_minor,
        "chargeback_fee_minor": event.chargeback_fee_minor,
        "entity_version": event.entity_version,
        "updated_at": event.occurred_at,
    }
    session.execute(
        insert(DisputeWorkflow)
        .values(**snapshot)
        .on_conflict_do_update(
            index_elements=["dispute_id"],
            set_={
                name: value for name, value in snapshot.items() if name not in ("dispute_id", "merchant_id")
            },
            where=(DisputeWorkflow.entity_version < event.entity_version)
            & (DisputeWorkflow.merchant_id == event.merchant_id),
        )
    )
    # ON CONFLICT locks the existing row even when the WHERE is false, so this read cannot race a writer.
    stored_merchant = session.execute(
        select(DisputeWorkflow.merchant_id).where(DisputeWorkflow.dispute_id == event.dispute_id)
    ).scalar_one()
    if stored_merchant != event.merchant_id:
        logger.warning(
            "dropped dispute event for another merchant dispute_id=%s event_id=%s",
            event.dispute_id,
            event.event_id,
        )
        return True
    session.execute(
        insert(DisputeWorkflowStep)
        .values(
            dispute_id=event.dispute_id,
            entity_version=event.entity_version,
            event_id=event.event_id,
            event_type=event.event_type,
            from_state=event.from_state,
            to_state=event.to_state,
            actor_role=event.actor_role,
            occurred_at=event.occurred_at,
        )
        .on_conflict_do_nothing(index_elements=["dispute_id", "entity_version"])
    )
    return True


def consume_disputes(
    session_factory: sessionmaker[Session],
    redis: Redis,
    clock: Clock,
    consumer_name: str,
    count: int = 100,
    block_ms: int | None = None,
    start_id: str = ">",
) -> int:
    """Reads gw.disputes.v1 via DISPUTE_WORKFLOWS_GROUP, applies each event, commits, then XACKs. Returns the
    number of entries read.

    An entry that cannot be parsed (any error: a bad shape, out-of-range values, a payload that breaks the
    JSON parser) or that the database rejects (DataError / IntegrityError) goes to DISPUTES_DEAD_LETTER_TOPIC
    and is XACKed, so no entry is retried forever. Other errors applying an event (the database or Redis
    being unavailable) propagate so the worker retries.
    """
    ensure_group(redis, DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP)
    response = redis.xreadgroup(
        DISPUTE_WORKFLOWS_GROUP, consumer_name, {DISPUTES_TOPIC: start_id}, count=count, block=block_ms
    )
    entries = _stream_entries(response)
    for entry_id, fields in entries:
        payload = fields.get("payload", "")
        try:
            event = DisputeEventV1.model_validate(json.loads(fields["payload"]))
        except Exception as error:  # any unparsable entry is dead-lettered
            _dead_letter(redis, entry_id, payload, error)
            continue
        try:
            with session_factory.begin() as session:
                apply_dispute_event(session, clock, event)
        except (DataError, IntegrityError) as error:
            _dead_letter(redis, entry_id, payload, error)
            continue
        redis.xack(DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP, entry_id)
    return len(entries)


def _dead_letter(redis: Redis, entry_id: str, payload: str, error: Exception) -> None:
    """Keeps the entry id, the error class, the payload's top-level keys and its SHA-256: never the payload
    or the error message, which can carry personal data. The source entry stays in gw.disputes.v1 until it
    is trimmed."""
    error_name = type(error).__name__
    redis.xadd(
        DISPUTES_DEAD_LETTER_TOPIC,
        {
            "source_entry_id": entry_id,
            "error": error_name,
            "payload_keys": _payload_keys(payload),
            "payload_sha256": hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest(),
        },
        maxlen=DEAD_LETTER_MAXLEN,
    )
    redis.xack(DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP, entry_id)
    logger.warning("dead-lettered dispute event entry_id=%s error=%s", entry_id, error_name)


def _payload_keys(payload: str) -> str:
    try:
        parsed = json.loads(payload)
    except Exception:  # best effort: the error class is recorded
        return ""
    if not isinstance(parsed, dict):
        return ""
    return ",".join(sorted(str(key) for key in parsed))


def _stream_entries(response: object) -> list[tuple[str, dict[str, str]]]:
    entries: list[tuple[str, dict[str, str]]] = []
    if not isinstance(response, list):
        return entries
    for stream_reply in response:
        _stream, batch = stream_reply
        for entry_id, fields in batch:
            entries.append((str(entry_id), {str(k): str(v) for k, v in fields.items()}))
    return entries
