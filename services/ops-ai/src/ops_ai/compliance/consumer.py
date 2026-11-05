"""Consumes gw.compliance.v1 (R2-10): a KYC review opens a compliance ticket; an erasure pseudonymises the
user's copies in ops-ai (transfer facts, ticket text) and records the request without the user id."""

import json
import logging
import uuid

from contracts.compliance import (
    COMPLIANCE_TOPIC,
    KycReviewRequiredV1,
    SubjectErasedV1,
    parse_compliance_event,
)
from pydantic import ValidationError
from redis import Redis
from sqlalchemy import func, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.compliance.models import SubjectErasure
from ops_ai.ingest.consumer import DEAD_LETTER_MAXLEN, ensure_group
from ops_ai.ingest.models import ProcessedEvent
from ops_ai.tickets.models import Ticket
from ops_ai.tickets.routing import route_ticket
from ops_ai.transfers.models import TransferFact

logger = logging.getLogger(__name__)

COMPLIANCE_GROUP = "ops-ai.compliance"
COMPLIANCE_DEAD_LETTER_TOPIC = "gw.compliance.v1.dead"
KYC_REVIEW_TITLE = "KYC re-verification required"

ComplianceEvent = KycReviewRequiredV1 | SubjectErasedV1


def apply_compliance_event(session: Session, clock: Clock, event: ComplianceEvent) -> bool:
    """Dedupes on processed_events(COMPLIANCE_GROUP, event_id) in the caller's transaction; False for a
    duplicate. Does not commit."""
    first_delivery = session.execute(
        insert(ProcessedEvent)
        .values(consumer=COMPLIANCE_GROUP, event_id=event.event_id, processed_at=clock())
        .on_conflict_do_nothing()
        .returning(ProcessedEvent.event_id)
    ).scalar_one_or_none()
    if first_delivery is None:
        return False
    if isinstance(event, KycReviewRequiredV1):
        _open_review_ticket(session, clock, event)
    else:
        _pseudonymise_subject(session, clock, event)
    return True


def _open_review_ticket(session: Session, clock: Clock, event: KycReviewRequiredV1) -> None:
    queue, priority = route_ticket("kyc_review")
    session.execute(
        insert(Ticket)
        .values(
            ticket_id=uuid.uuid4(),
            source_key=f"kyc_review:{event.review_id}",
            source="kyc_review",
            queue=queue,
            priority=priority,
            merchant_id=None,
            title=KYC_REVIEW_TITLE,
            reason=f"User {event.user_id} needs re-verification: {', '.join(event.rules)}",
            created_at=clock(),
        )
        .on_conflict_do_nothing(index_elements=["source_key"])
    )


def _pseudonymise_subject(session: Session, clock: Clock, event: SubjectErasedV1) -> None:
    """One fresh random pseudonym per erasure replaces the user id in transfer facts and in ticket text, so
    the user's facts still group together but no longer join back to the gateway user."""
    pseudonym = uuid.uuid4()
    changed = session.scalars(
        update(TransferFact)
        .where(TransferFact.sender_user_id == event.user_id)
        .values(sender_user_id=pseudonym)
        .returning(TransferFact.transfer_id)
        .execution_options(synchronize_session=False)
    ).all()
    rows = len(changed)
    original, replacement = str(event.user_id), str(pseudonym)
    session.execute(
        update(Ticket)
        .where(or_(Ticket.title.contains(original), Ticket.reason.contains(original)))
        .values(
            title=func.replace(Ticket.title, original, replacement),
            reason=func.replace(Ticket.reason, original, replacement),
        )
        .execution_options(synchronize_session=False)
    )
    session.execute(
        insert(SubjectErasure)
        .values(request_id=event.request_id, processed_at=clock(), rows_pseudonymised=rows)
        .on_conflict_do_nothing(index_elements=["request_id"])
    )


def consume_compliance(
    session_factory: sessionmaker[Session],
    redis: Redis,
    clock: Clock,
    consumer_name: str,
    count: int = 100,
    block_ms: int | None = None,
    start_id: str = ">",
) -> int:
    """Reads gw.compliance.v1 via COMPLIANCE_GROUP, applies each event, commits, then XACKs. Returns the
    number of entries read. A malformed entry is copied to COMPLIANCE_DEAD_LETTER_TOPIC and XACKed; errors
    applying a valid event propagate so the worker retries."""
    ensure_group(redis, COMPLIANCE_TOPIC, COMPLIANCE_GROUP)
    response = redis.xreadgroup(
        COMPLIANCE_GROUP, consumer_name, {COMPLIANCE_TOPIC: start_id}, count=count, block=block_ms
    )
    entries = _stream_entries(response)
    for entry_id, fields in entries:
        try:
            payload = json.loads(fields["payload"])
            if not isinstance(payload, dict):
                raise ValueError("payload is not a JSON object")
            event = parse_compliance_event(payload)
        except (KeyError, ValueError, ValidationError) as error:
            _dead_letter(redis, entry_id, fields.get("payload", ""), error)
            continue
        with session_factory.begin() as session:
            apply_compliance_event(session, clock, event)
        redis.xack(COMPLIANCE_TOPIC, COMPLIANCE_GROUP, entry_id)
    return len(entries)


def _dead_letter(redis: Redis, entry_id: str, payload: str, error: Exception) -> None:
    """Logs the entry id and error class only: a rejected payload can carry personal data."""
    error_name = type(error).__name__
    redis.xadd(
        COMPLIANCE_DEAD_LETTER_TOPIC,
        {"source_entry_id": entry_id, "error": error_name, "payload": payload},
        maxlen=DEAD_LETTER_MAXLEN,
    )
    redis.xack(COMPLIANCE_TOPIC, COMPLIANCE_GROUP, entry_id)
    logger.warning("dead-lettered compliance event entry_id=%s error=%s", entry_id, error_name)


def _stream_entries(response: object) -> list[tuple[str, dict[str, str]]]:
    entries: list[tuple[str, dict[str, str]]] = []
    if not isinstance(response, list):
        return entries
    for stream_reply in response:
        _stream, batch = stream_reply
        for entry_id, fields in batch:
            entries.append((str(entry_id), {str(k): str(v) for k, v in fields.items()}))
    return entries
