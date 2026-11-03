import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class ProcessedEvent(OpsAiBase):
    """Consumer dedupe: written in the same transaction as the projection change."""

    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime]


class PaymentFact(OpsAiBase):
    __tablename__ = "payment_facts"

    payment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(32))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    card_bin: Mapped[str | None] = mapped_column(String(8))
    entity_version: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime]
