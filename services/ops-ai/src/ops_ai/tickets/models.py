import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase

QUEUES = ("fraud", "payments_ops", "settlement", "compliance")
PRIORITIES = ("high", "normal", "low")
STATUSES = ("open", "assigned", "resolved")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Ticket(OpsAiBase):
    """One ticket per source (`source_key`, e.g. an alert's window key)."""

    __tablename__ = "tickets"
    __table_args__ = (
        CheckConstraint(_one_of("queue", QUEUES), name="queue"),
        CheckConstraint(_one_of("priority", PRIORITIES), name="priority"),
        CheckConstraint(_one_of("status", STATUSES), name="status"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    source_key: Mapped[str] = mapped_column(Text, unique=True)
    queue: Mapped[str] = mapped_column(String(32))
    priority: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), server_default="open")
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    alert_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ops_ai.fraud_alerts.alert_id"))
    created_at: Mapped[datetime]
