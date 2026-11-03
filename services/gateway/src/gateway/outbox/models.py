import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Identity, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase


class OutboxEvent(GatewayBase):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_unpublished", "id", postgresql_where=text("published_at IS NULL")),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    headers: Mapped[dict[str, str]] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    published_at: Mapped[datetime | None]
