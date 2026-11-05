import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class OpsAlert(OpsAiBase):
    """An ops staff alert whose title and body contain counts and reasons, never payment ids or card refs."""

    __tablename__ = "ops_alerts"
    __table_args__ = (
        Index("ix_ops_alerts_merchant_id", "merchant_id"),
        Index("ix_ops_alerts_created_at", "created_at"),
        CheckConstraint("severity IN ('high', 'medium', 'low')", name="severity"),
    )

    alert_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    merchant_id: Mapped[uuid.UUID]
    fraud_alert_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    acknowledged_at: Mapped[datetime | None]
    acknowledged_by: Mapped[uuid.UUID | None]


class OpsAlertDelivery(OpsAiBase):
    __tablename__ = "ops_alert_deliveries"
    __table_args__ = (
        UniqueConstraint("alert_id", "channel"),
        Index("ix_ops_alert_deliveries_status_created_at", "status", "created_at"),
        CheckConstraint("status IN ('pending', 'delivered', 'failed')", name="status"),
    )

    delivery_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    alert_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ops_ai.ops_alerts.alert_id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    stream_entry_id: Mapped[str | None] = mapped_column(String(64))
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime]
    delivered_at: Mapped[datetime | None]
