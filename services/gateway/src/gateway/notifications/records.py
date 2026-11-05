"""In-app notification records written inside the caller's transaction (no outbox row).

The `gw.notifications.v1` outbox contract covers only the notifier's merchant kinds; the records here are
read through the notification APIs.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gateway.notifications.models import Notification

RECORD_NAMESPACE = uuid.UUID("0c8f3a7e-5d21-4b9c-8e64-2f7a1d9b3c50")
IN_APP_CHANNEL = "in_app"

RecipientType = Literal["merchant", "user"]
RecordKind = Literal[
    "payment_receipt",
    "was_this_you",
    "dispute_opened",
    "dispute_resolved",
    "dispute_update",
    "refund_requested",
]


def record_id(source_id: uuid.UUID, kind: RecordKind, recipient_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(RECORD_NAMESPACE, f"{source_id}:{kind}:{recipient_id}")


def add_notification(
    session: Session,
    now: datetime,
    kind: RecordKind,
    recipient_type: RecipientType,
    recipient_id: uuid.UUID,
    payment_id: uuid.UUID,
    source_id: uuid.UUID,
    payload: dict[str, object],
) -> bool:
    """Inserts one record; False if (source_id, kind) already has one. Payloads must stay pseudonymous."""
    inserted = session.execute(
        insert(Notification)
        .values(
            id=record_id(source_id, kind, recipient_id),
            kind=kind,
            recipient_type=recipient_type,
            recipient_id=recipient_id,
            source_event_id=source_id,
            payment_id=payment_id,
            channel=IN_APP_CHANNEL,
            payload=payload,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(Notification.id)
    ).scalar_one_or_none()
    return inserted is not None
