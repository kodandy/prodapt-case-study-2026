import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase


class Notification(GatewayBase):
    """In-app notification record. Pseudonymous ids only: no PAN, BIN, last4 or email in `payload`."""

    __tablename__ = "notifications"
    __table_args__ = (
        UniqueConstraint("source_event_id", "kind"),
        Index("ix_notifications_recipient_created", "recipient_id", "created_at"),
        Index("ix_notifications_recipient_feed", "recipient_type", "recipient_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    recipient_type: Mapped[str] = mapped_column(String(16))
    recipient_id: Mapped[uuid.UUID]
    source_event_id: Mapped[uuid.UUID]
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"))
    channel: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    read_at: Mapped[datetime | None]
    response: Mapped[str | None] = mapped_column(String(16))
    """`confirmed | not_me`, only on `was_this_you`."""
    responded_at: Mapped[datetime | None]


class NotificationProcessedEvent(GatewayBase):
    """A payment outbox event the notifier has handled, with or without creating records."""

    __tablename__ = "notification_processed_events"

    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime]
