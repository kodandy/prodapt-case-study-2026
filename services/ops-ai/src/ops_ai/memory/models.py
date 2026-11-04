import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class MerchantMemoryRecord(OpsAiBase):
    """Append-only: a correction is a new row (`supersedes_id`); a delete sets `deleted_at`.

    Active = `superseded_at` and `deleted_at` both NULL.
    """

    __tablename__ = "merchant_memory"
    __table_args__ = (
        CheckConstraint("char_length(content) BETWEEN 1 AND 500", name="content_length"),
        Index("ix_merchant_memory_merchant_created", "merchant_id", "created_at"),
        Index(
            "uq_merchant_memory_source_turn",
            "merchant_id",
            "source_turn_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
    )

    memory_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID]
    created_by: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    source_session_id: Mapped[uuid.UUID]
    source_turn_id: Mapped[uuid.UUID]
    supersedes_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]
    superseded_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    deleted_by: Mapped[str | None] = mapped_column(String(128))
