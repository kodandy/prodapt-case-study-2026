import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.notify.models import AlertNotification

ALERT_NOTIFICATION_SCHEMA = "payplat.fraud_alert.v1"
NOTIFIED_FEATURES = (
    "micro_payments",
    "card_payments",
    "micro_share",
    "distinct_cards",
    "declined",
    "decline_ratio",
    "currencies",
    "reasons",
)
MAX_NOTIFICATION_ATTEMPTS = 5
_NOTIFICATION_NAMESPACE = uuid.UUID("3b8e4f1a-7c2d-4e9b-a6f0-5d1c8b2e7a94")


class AlertSinkError(Exception):
    """A failed send. `reason` is short and fixed-form: never a URL, a header or the body."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AlertNotificationMessage:
    notification_id: uuid.UUID
    body: bytes


class AlertSink(Protocol):
    def send(self, message: AlertNotificationMessage, now: datetime) -> None: ...


def alert_notification_id(fraud_alert_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_NOTIFICATION_NAMESPACE, f"alert_notification:{fraud_alert_id}")


def enqueue_alert_notification(
    session: Session,
    clock: Clock,
    *,
    fraud_alert_id: uuid.UUID,
    merchant_id: uuid.UUID,
    kind: str,
    payment_count: int,
    features: Mapping[str, object],
) -> uuid.UUID | None:
    """Adds the pending outbox row for a fraud alert, once per alert. Does not commit; no network call."""
    now = clock()
    notification_id = alert_notification_id(fraud_alert_id)
    payload: dict[str, object] = {
        "schema": ALERT_NOTIFICATION_SCHEMA,
        "notification_id": str(notification_id),
        "alert_id": str(fraud_alert_id),
        "kind": kind,
        "merchant_id": str(merchant_id),
        "payment_count": payment_count,
        "features": {key: features[key] for key in NOTIFIED_FEATURES if key in features},
        "raised_at": now.isoformat(),
    }
    return session.execute(
        insert(AlertNotification)
        .values(
            notification_id=notification_id,
            fraud_alert_id=fraud_alert_id,
            merchant_id=merchant_id,
            payload=payload,
            status="pending",
            attempts=0,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[AlertNotification.fraud_alert_id])
        .returning(AlertNotification.notification_id)
    ).scalar_one_or_none()


def deliver_alert_notifications(
    session_factory: sessionmaker[Session], sink: AlertSink, clock: Clock, limit: int = 100
) -> int:
    """Sends pending notifications oldest first; returns the number delivered. At least once: a crash
    after the send and before the commit resends, and the receiver dedupes on the notification id."""
    delivered = 0
    with session_factory.begin() as session:
        rows = session.scalars(
            select(AlertNotification)
            .where(AlertNotification.status == "pending")
            .order_by(AlertNotification.created_at, AlertNotification.notification_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            body = json.dumps(row.payload, sort_keys=True, separators=(",", ":")).encode()
            try:
                sink.send(AlertNotificationMessage(row.notification_id, body), clock())
            except AlertSinkError as error:
                row.attempts += 1
                row.last_error = error.reason[:64]
                if row.attempts >= MAX_NOTIFICATION_ATTEMPTS:
                    row.status = "failed"
                continue
            row.status = "delivered"
            row.delivered_at = clock()
            row.last_error = None
            delivered += 1
    return delivered
