import uuid
from datetime import UTC, datetime, timedelta

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from ops_ai.ingest.consumer import (
    PAYMENT_FACTS_GROUP,
    apply_payment_event,
    consume_payments,
    ensure_group,
)
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
FACTS_QUERY = (
    "SELECT payment_id, merchant_id, amount_minor, currency, method, state, provider, decline_code, "
    "card_bin, entity_version, occurred_at FROM ops_ai.payment_facts ORDER BY payment_id"
)


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _event(
    *,
    payment_id: uuid.UUID | None = None,
    merchant_id: uuid.UUID | None = None,
    declined: bool = True,
    entity_version: int = 1,
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=OCCURRED_AT + timedelta(seconds=entity_version),
        entity_version=entity_version,
        payment_id=payment_id or uuid.uuid4(),
        merchant_id=merchant_id or uuid.uuid4(),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0002" if declined else "4242",
    )


def _apply(session_factory: sessionmaker[Session], event: PaymentEventV1) -> bool:
    with session_factory() as session:
        applied = apply_payment_event(session, _clock_now, event)
        session.commit()
    return applied


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _facts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, FACTS_QUERY)


def _stream_add(redis: Redis, event: PaymentEventV1) -> None:
    redis.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})


def test_apply_payment_event_creates_the_payment_fact(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    event = _event()

    # Act
    applied = _apply(session_factory, event)

    # Assert
    assert applied is True
    assert _facts(session_factory) == [
        {
            "payment_id": event.payment_id,
            "merchant_id": event.merchant_id,
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "state": "declined",
            "provider": "acq_a",
            "decline_code": "card_declined",
            "card_bin": "400000",
            "entity_version": 1,
            "occurred_at": event.occurred_at,
        }
    ]


def test_apply_payment_event_creates_a_search_document_for_a_declined_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(declined=True)

    # Act
    _apply(session_factory, event)

    # Assert
    documents = _rows(
        session_factory, "SELECT kind, ref_id, merchant_id, title, body FROM ops_ai.search_documents"
    )
    assert len(documents) == 1
    document = documents[0]
    assert (document["kind"], document["ref_id"], document["merchant_id"]) == (
        "payment",
        event.payment_id,
        event.merchant_id,
    )
    assert document["title"] == "Card payment declined"
    body = str(document["body"])
    assert "card_declined" in body
    assert "acq_a" in body
    assert "USD" in body


def test_apply_payment_event_search_document_for_a_success_has_no_decline_code(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event(declined=False)

    # Act
    _apply(session_factory, event)

    # Assert
    documents = _rows(session_factory, "SELECT title, body FROM ops_ai.search_documents")
    assert len(documents) == 1
    assert documents[0]["title"] == "Card payment succeeded"
    body = str(documents[0]["body"])
    assert "acq_a" in body
    assert "USD" in body
    assert "None" not in body


def test_apply_payment_event_returns_false_for_the_same_event_and_changes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _event()
    _apply(session_factory, event)
    facts_before = _facts(session_factory)

    # Act
    second = _apply(session_factory, event)

    # Assert
    assert second is False
    assert _facts(session_factory) == facts_before
    assert _rows(session_factory, "SELECT count(*) AS n FROM ops_ai.search_documents") == [{"n": 1}]
    assert _rows(session_factory, "SELECT count(*) AS n FROM ops_ai.processed_events") == [{"n": 1}]


def test_apply_payment_event_does_not_overwrite_a_higher_entity_version(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    payment_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    newer = _event(payment_id=payment_id, merchant_id=merchant_id, declined=True, entity_version=2)
    older = _event(payment_id=payment_id, merchant_id=merchant_id, declined=False, entity_version=1)
    _apply(session_factory, newer)

    # Act
    _apply(session_factory, older)

    # Assert
    facts = _facts(session_factory)
    assert len(facts) == 1
    assert (facts[0]["state"], facts[0]["decline_code"], facts[0]["entity_version"]) == (
        "declined",
        "card_declined",
        2,
    )
    titles = _rows(session_factory, "SELECT title FROM ops_ai.search_documents")
    assert titles == [{"title": "Card payment declined"}]


def test_apply_payment_event_overwrites_a_lower_entity_version(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    payment_id = uuid.uuid4()
    merchant_id = uuid.uuid4()
    older = _event(payment_id=payment_id, merchant_id=merchant_id, declined=False, entity_version=1)
    newer = _event(payment_id=payment_id, merchant_id=merchant_id, declined=True, entity_version=2)
    _apply(session_factory, older)

    # Act
    _apply(session_factory, newer)

    # Assert
    facts = _facts(session_factory)
    assert len(facts) == 1
    assert (facts[0]["state"], facts[0]["decline_code"], facts[0]["entity_version"]) == (
        "declined",
        "card_declined",
        2,
    )
    titles = _rows(session_factory, "SELECT title FROM ops_ai.search_documents")
    assert titles == [{"title": "Card payment declined"}]


def test_ensure_group_is_idempotent(redis_client: Redis) -> None:
    # Arrange / Act
    ensure_group(redis_client, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
    ensure_group(redis_client, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)

    # Assert
    groups = redis_client.xinfo_groups(PAYMENTS_TOPIC)
    assert [group["name"] for group in groups] == [PAYMENT_FACTS_GROUP]


def test_consume_payments_applies_stream_events_and_acknowledges_them(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    ensure_group(redis_client, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
    declined = _event(declined=True)
    succeeded = _event(declined=False)
    _stream_add(redis_client, declined)
    _stream_add(redis_client, succeeded)

    # Act
    read = consume_payments(session_factory, redis_client, _clock_now, "consumer-1")

    # Assert
    facts = _facts(session_factory)
    assert read == 2
    assert {fact["payment_id"]: fact["state"] for fact in facts} == {
        declined.payment_id: "declined",
        succeeded.payment_id: "succeeded",
    }
    assert redis_client.xpending(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)["pending"] == 0


def test_consume_payments_redelivery_of_the_same_event_changes_nothing(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    ensure_group(redis_client, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
    event = _event()
    _stream_add(redis_client, event)
    consume_payments(session_factory, redis_client, _clock_now, "consumer-1")
    facts_before = _facts(session_factory)
    _stream_add(redis_client, event)

    # Act
    read = consume_payments(session_factory, redis_client, _clock_now, "consumer-1")

    # Assert
    assert read == 1
    assert _facts(session_factory) == facts_before
    assert _rows(session_factory, "SELECT count(*) AS n FROM ops_ai.search_documents") == [{"n": 1}]
    assert redis_client.xpending(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)["pending"] == 0


def test_consume_payments_on_an_empty_stream_reads_nothing(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    ensure_group(redis_client, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)

    # Act
    read = consume_payments(session_factory, redis_client, _clock_now, "consumer-1")

    # Assert
    assert read == 0
    assert _facts(session_factory) == []
