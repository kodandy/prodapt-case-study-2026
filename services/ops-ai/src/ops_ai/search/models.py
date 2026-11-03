import uuid
from datetime import datetime

from sqlalchemy import Computed, Index, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class SearchDocument(OpsAiBase):
    """kind: payment | incident | resolution_note. One row per (kind, ref_id)."""

    __tablename__ = "search_documents"
    __table_args__ = (Index("ix_search_documents_tsv", "tsv", postgresql_using="gin"),)

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    ref_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', title || ' ' || body)", persisted=True)
    )
    occurred_at: Mapped[datetime]
