import uuid
from datetime import datetime

from sqlalchemy import Computed, Index, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class IncidentRecord(OpsAiBase):
    """merchant_id NULL = platform-wide: visible to every merchant."""

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_tsv", "tsv", postgresql_using="gin"),)

    incident_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(Text)
    root_cause: Mapped[str] = mapped_column(Text)
    fix_applied: Mapped[str] = mapped_column(Text)
    resolution_notes: Mapped[str] = mapped_column(Text)
    started_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    recurrence_within_7d: Mapped[bool]
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', title || ' ' || root_cause || ' ' || fix_applied)", persisted=True),
    )
