import json
import logging

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from pydantic import ValidationError
from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.analytics.anomaly import observe_payment
from ops_ai.clock import Clock
from ops_ai.fraud_watch.watch import watch_payment
from ops_ai.ingest.models import PaymentFact, ProcessedEvent
from ops_ai.search.embedder import LocalHashEmbedder, document_text, is_zero
from ops_ai.search.models import SearchDocument

logger = logging.getLogger(__name__)

PAYMENT_FACTS_GROUP = "ops-ai.payment-facts"
PAYMENTS_DEAD_LETTER_TOPIC = "gw.payments.v1.dead"
DEAD_LETTER_MAXLEN = 10_000
_LOCAL_EMBEDDER = LocalHashEmbedder()


def ensure_group(redis: Redis, stream: str, group: str) -> None:
    """Creates the consumer group (and the stream) if missing. Idempotent."""
    try:
        redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


def apply_payment_event(session: Session, clock: Clock, event: PaymentEventV1) -> bool:
    """Upserts payment_facts (highest entity_version wins) and the payment search document (with its
    local-hash-v1 embedding; other embedders' columns are filled by `python -m ops_ai.search backfill`),
    then runs the card-testing watch on the projected payment.

    Dedupes on processed_events(consumer, event_id) in the same transaction. Returns False for a duplicate.
    Does not commit.
    """
    first_delivery = session.execute(
        insert(ProcessedEvent)
        .values(consumer=PAYMENT_FACTS_GROUP, event_id=event.event_id, processed_at=clock())
        .on_conflict_do_nothing()
        .returning(ProcessedEvent.event_id)
    ).scalar_one_or_none()
    if first_delivery is None:
        return False
    current = session.execute(
        select(PaymentFact.entity_version, PaymentFact.merchant_id)
        .where(PaymentFact.payment_id == event.payment_id)
        .with_for_update()
    ).one_or_none()
    if current is not None and (
        current.merchant_id != event.merchant_id or current.entity_version >= event.entity_version
    ):
        # A payment's merchant is immutable once projected; a conflicting event is consumed and dropped.
        return True
    fact = {
        "payment_id": event.payment_id,
        "merchant_id": event.merchant_id,
        "amount_minor": event.amount_minor,
        "currency": event.currency,
        "method": event.method,
        "state": event.state,
        "provider": event.provider,
        "decline_code": event.decline_code,
        "card_bin": event.card_bin,
        "card_ref": event.card_ref,
        "entity_version": event.entity_version,
        "occurred_at": event.occurred_at,
    }
    session.execute(
        insert(PaymentFact).values(**fact).on_conflict_do_update(index_elements=["payment_id"], set_=fact)
    )
    title = f"Card payment {event.state}"
    body = _payment_body(event)
    [embedding] = _LOCAL_EMBEDDER.embed([document_text(title, body)])
    document = {
        "kind": "payment",
        "ref_id": event.payment_id,
        "merchant_id": event.merchant_id,
        "title": title,
        "body": body,
        "occurred_at": event.occurred_at,
        "embedding_local_hash_v1": None if is_zero(embedding) else embedding,
    }
    session.execute(
        insert(SearchDocument)
        .values(**document)
        .on_conflict_do_update(index_elements=["kind", "ref_id"], set_=document)
    )
    watch_payment(session, clock, event)
    _observe_isolated(session, clock, event)
    return True


def _observe_isolated(session: Session, clock: Clock, event: PaymentEventV1) -> None:
    """Anomaly detection is best effort: a failure rolls back its savepoint and never blocks ingest."""
    try:
        with session.begin_nested():
            observe_payment(session, clock, event)
    except (ArithmeticError, ValueError, SQLAlchemyError) as error:
        logger.warning("anomaly observe skipped for event %s: %s", event.event_id, type(error).__name__)


def _payment_body(event: PaymentEventV1) -> str:
    parts = [
        f"{event.method} payment {event.state}",
        f"amount {event.amount_minor} {event.currency} (minor units)",
        f"provider {event.provider or 'none'}",
        f"BIN {event.card_bin or 'none'}",
    ]
    if event.decline_code:
        parts.append(f"decline code {event.decline_code}")
    return "; ".join(parts)


def consume_payments(
    session_factory: sessionmaker[Session],
    redis: Redis,
    clock: Clock,
    consumer_name: str,
    count: int = 100,
    block_ms: int | None = None,
    start_id: str = ">",
) -> int:
    """Reads gw.payments.v1 via PAYMENT_FACTS_GROUP, applies each event, commits, then XACKs.

    `start_id` ">" reads new entries; "0" re-reads this consumer's unacked entries (after a failed apply).
    Returns the number of entries read (duplicates and dead-lettered entries included).
    A malformed entry (no payload, not JSON, or rejected by PaymentEventV1) is copied to
    PAYMENTS_DEAD_LETTER_TOPIC and XACKed; errors applying a valid event propagate so the worker retries.
    """
    ensure_group(redis, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
    response = redis.xreadgroup(
        PAYMENT_FACTS_GROUP, consumer_name, {PAYMENTS_TOPIC: start_id}, count=count, block=block_ms
    )
    entries = _stream_entries(response)
    for entry_id, fields in entries:
        try:
            event = PaymentEventV1.model_validate(json.loads(fields["payload"]))
        except (KeyError, ValueError, ValidationError) as error:
            _dead_letter(redis, entry_id, fields.get("payload", ""), error)
            continue
        with session_factory.begin() as session:
            apply_payment_event(session, clock, event)
        redis.xack(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP, entry_id)
    return len(entries)


def _dead_letter(redis: Redis, entry_id: str, payload: str, error: Exception) -> None:
    """Logs the entry id and error class only: the payload and the validation message can carry card data."""
    error_name = type(error).__name__
    redis.xadd(
        PAYMENTS_DEAD_LETTER_TOPIC,
        {"source_entry_id": entry_id, "error": error_name, "payload": payload},
        maxlen=DEAD_LETTER_MAXLEN,
    )
    redis.xack(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP, entry_id)
    logger.warning("dead-lettered event entry_id=%s error=%s", entry_id, error_name)


StreamEntry = tuple[str, dict[str, str]]


def _stream_entries(response: object) -> list[StreamEntry]:
    """Narrows redis-py's untyped XREADGROUP reply ([[stream, [(id, fields), ...]], ...])."""
    entries: list[StreamEntry] = []
    if not isinstance(response, list):
        return entries
    for stream_reply in response:
        _stream, batch = stream_reply
        for entry_id, fields in batch:
            entries.append((str(entry_id), {str(k): str(v) for k, v in fields.items()}))
    return entries
