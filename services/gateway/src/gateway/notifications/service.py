import logging
import uuid
from datetime import datetime

from contracts.events import (
    NOTIFICATIONS_TOPIC,
    PAYMENTS_TOPIC,
    NotificationEventV1,
    NotificationKind,
    PaymentEventV1,
)
from pydantic import ValidationError
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.notifications.models import Notification, NotificationProcessedEvent
from gateway.notifications.records import add_notification
from gateway.outbox.models import OutboxEvent
from gateway.payments.models import Payment

NOTIFICATION_NAMESPACE = uuid.UUID("6f1d7c52-2b8e-4f0a-9a43-5d0b8e6c1f27")
IN_APP_CHANNEL = "in_app"

logger = logging.getLogger("gateway.notifications")


def notification_id(source_event_id: uuid.UUID, kind: NotificationKind) -> uuid.UUID:
    return uuid.uuid5(NOTIFICATION_NAMESPACE, f"{source_event_id}:{kind}")


def process_pending_notifications(
    session_factory: sessionmaker[Session], clock: Clock, batch_size: int = 100
) -> int:
    """Turns unprocessed payment outbox events into notification records; returns the number created.

    Each source event is claimed by inserting its id into `notification_processed_events` in the same
    transaction as its records, so a concurrent or repeated pass skips it.
    """
    with session_factory.begin() as session:
        rows = session.execute(
            select(OutboxEvent.event_id, OutboxEvent.payload)
            .where(
                OutboxEvent.topic == PAYMENTS_TOPIC,
                ~exists().where(NotificationProcessedEvent.event_id == OutboxEvent.event_id),
            )
            .order_by(OutboxEvent.id)
            .limit(batch_size)
        ).all()
        now = clock()
        created = 0
        for row in rows:
            claimed = session.execute(
                insert(NotificationProcessedEvent)
                .values(event_id=row.event_id, processed_at=now)
                .on_conflict_do_nothing()
                .returning(NotificationProcessedEvent.event_id)
            ).scalar_one_or_none()
            if claimed is None:
                continue
            created += _notify(session, now, row.event_id, row.payload)
        return created


def _notify(session: Session, now: datetime, outbox_event_id: uuid.UUID, payload: object) -> int:
    try:
        event = PaymentEventV1.model_validate(payload)
    except ValidationError:
        logger.warning("notifier skipped invalid payment event %s", outbox_event_id)
        return 0
    if event.origin == "backfill":
        return 0
    if event.event_id != outbox_event_id:
        logger.warning("notifier skipped event %s: payload event id mismatch", outbox_event_id)
        return 0
    payment = session.get(Payment, event.payment_id)
    if payment is None:
        logger.warning("notifier skipped event %s: payment not found", event.event_id)
        return 0
    if payment.merchant_id != event.merchant_id:
        logger.warning("notifier skipped event %s: merchant mismatch", event.event_id)
        return 0
    amount = {"amount_minor": payment.amount_minor, "currency": payment.currency}
    created = 0
    if event.event_type == "payment.succeeded":
        created += _create(session, now, event, payment, "payment_confirmation", amount)
    if payment.risk_flagged or payment.risk_blocked:
        # Score and reasons draw on the card's activity at every merchant, so they stay internal.
        alert = {**amount, "blocked": payment.risk_blocked}
        created += _create(session, now, event, payment, "suspicious_activity", alert)
    if payment.payer_user_id is not None:
        created += _notify_payer(session, now, event, payment, payment.payer_user_id)
    return created


def _notify_payer(
    session: Session, now: datetime, event: PaymentEventV1, payment: Payment, payer_user_id: uuid.UUID
) -> int:
    """In-app records for the paying user (no outbox row): a receipt, and a "was this you?" notice when
    the risk rules flagged or blocked the payment."""
    payload: dict[str, object] = {
        "amount_minor": payment.amount_minor,
        "currency": payment.currency,
        "merchant_id": str(payment.merchant_id),
    }
    created = 0
    if event.event_type == "payment.succeeded" and add_notification(
        session, now, "payment_receipt", "user", payer_user_id, payment.id, event.event_id, payload
    ):
        created += 1
    if (payment.risk_flagged or payment.risk_blocked) and add_notification(
        session,
        now,
        "was_this_you",
        "user",
        payer_user_id,
        payment.id,
        event.event_id,
        {**payload, "blocked": payment.risk_blocked},
    ):
        created += 1
    return created


def _create(
    session: Session,
    now: datetime,
    event: PaymentEventV1,
    payment: Payment,
    kind: NotificationKind,
    payload: dict[str, object],
) -> int:
    record_id = notification_id(event.event_id, kind)
    inserted = session.execute(
        insert(Notification)
        .values(
            id=record_id,
            kind=kind,
            recipient_type="merchant",
            recipient_id=payment.merchant_id,
            source_event_id=event.event_id,
            payment_id=payment.id,
            channel=IN_APP_CHANNEL,
            payload=payload,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(Notification.id)
    ).scalar_one_or_none()
    if inserted is None:
        return 0
    notification_event = NotificationEventV1(
        event_id=uuid.uuid5(NOTIFICATION_NAMESPACE, f"event:{record_id}"),
        event_type="notification.created",
        occurred_at=event.occurred_at,
        notification_id=record_id,
        kind=kind,
        source_event_id=event.event_id,
        recipient_type="merchant",
        recipient_id=payment.merchant_id,
        payment_id=payment.id,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
    )
    session.execute(
        insert(OutboxEvent)
        .values(
            event_id=notification_event.event_id,
            topic=NOTIFICATIONS_TOPIC,
            payload=notification_event.model_dump(mode="json"),
            headers={},
            created_at=now,
            published_at=None,
        )
        .on_conflict_do_nothing(index_elements=[OutboxEvent.event_id])
    )
    logger.info(
        "notification %s created: kind=%s payment=%s merchant=%s",
        record_id,
        kind,
        payment.id,
        payment.merchant_id,
    )
    return 1
