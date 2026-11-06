import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class AlertNotification(OpsAiBase):
    """Outbox row for the outbound notification of one fraud alert. `payload` holds ids, counts, ratios,
    currencies and reasons: never payment ids, card refs, BINs or last 4."""

    __tablename__ = "alert_notifications"
    __table_args__ = (
        Index("ix_alert_notifications_status_created_at", "status", "created_at"),
        CheckConstraint("status IN ('pending', 'delivered', 'failed')", name="status"),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    fraud_alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_ai.fraud_alerts.alert_id", ondelete="CASCADE"), unique=True
    )
    merchant_id: Mapped[uuid.UUID]
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]
