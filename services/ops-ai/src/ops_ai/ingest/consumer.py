import json

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from redis import Redis
from redis.exceptions import ResponseError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.ingest.models import PaymentFact, ProcessedEvent
from ops_ai.search.models import SearchDocument

PAYMENT_FACTS_GROUP = "ops-ai.payment-facts"


def ensure_group(redis: Redis, stream: str, group: str) -> None:
    """Creates the consumer group (and the stream) if missing. Idempotent."""
    try:
        redis.xgroup_create(stream, group, id="0", mkstream=True)
    except ResponseError as error:
        if "BUSYGROUP" not in str(error):
            raise


def apply_payment_event(session: Session, clock: Clock, event: PaymentEventV1) -> bool:
    """Upserts payment_facts (highest entity_version wins) and the payment search document.

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
    current = session.scalar(
        select(PaymentFact.entity_version).where(PaymentFact.payment_id == event.payment_id).with_for_update()
    )
    if current is not None and current >= event.entity_version:
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
        "entity_version": event.entity_version,
        "occurred_at": event.occurred_at,
    }
    session.execute(
        insert(PaymentFact).values(**fact).on_conflict_do_update(index_elements=["payment_id"], set_=fact)
    )
    document = {
        "kind": "payment",
        "ref_id": event.payment_id,
        "merchant_id": event.merchant_id,
        "title": f"Card payment {event.state}",
        "body": _payment_body(event),
        "occurred_at": event.occurred_at,
    }
    session.execute(
        insert(SearchDocument)
        .values(**document)
        .on_conflict_do_update(index_elements=["kind", "ref_id"], set_=document)
    )
    return True


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
) -> int:
    """Reads gw.payments.v1 via PAYMENT_FACTS_GROUP, applies each event, commits, then XACKs.

    Returns the number of entries read (duplicates included).
    """
    ensure_group(redis, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
    response = redis.xreadgroup(
        PAYMENT_FACTS_GROUP, consumer_name, {PAYMENTS_TOPIC: ">"}, count=count, block=block_ms
    )
    entries = _stream_entries(response)
    for entry_id, fields in entries:
        event = PaymentEventV1.model_validate(json.loads(fields["payload"]))
        with session_factory.begin() as session:
            apply_payment_event(session, clock, event)
        redis.xack(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP, entry_id)
    return len(entries)


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
