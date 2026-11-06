import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String
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
    __table_args__ = (
        Index("ix_ops_ai_payment_facts_merchant_method_occurred_at", "merchant_id", "method", "occurred_at"),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(32))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    card_bin: Mapped[str | None] = mapped_column(String(8))
    card_ref: Mapped[str | None] = mapped_column(String(32))
    entity_version: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime]


class DisputeWorkflow(OpsAiBase):
    """Projection of gw.disputes.v1: the latest snapshot of one gateway dispute (highest version wins)."""

    __tablename__ = "dispute_workflows"
    __table_args__ = (Index("ix_ops_ai_dispute_workflows_merchant_updated_at", "merchant_id", "updated_at"),)

    dispute_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID]
    payment_id: Mapped[uuid.UUID]
    network_reason_code: Mapped[str | None] = mapped_column(String(16))
    currency: Mapped[str] = mapped_column(String(3))
    payment_amount_minor: Mapped[int] = mapped_column(BigInteger)
    refunded_minor: Mapped[int] = mapped_column(BigInteger)
    dispute_amount_minor: Mapped[int] = mapped_column(BigInteger)
    chargeback_minor: Mapped[int] = mapped_column(BigInteger)
    chargeback_fee_minor: Mapped[int] = mapped_column(BigInteger)
    entity_version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime]


class DisputeWorkflowStep(OpsAiBase):
    """One transition of a projected dispute, keyed by its position in the workflow."""

    __tablename__ = "dispute_workflow_steps"

    dispute_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_ai.dispute_workflows.dispute_id", ondelete="CASCADE"), primary_key=True
    )
    entity_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[uuid.UUID]
    event_type: Mapped[str] = mapped_column(String(32))
    from_state: Mapped[str | None] = mapped_column(String(16))
    to_state: Mapped[str] = mapped_column(String(16))
    actor_role: Mapped[str] = mapped_column(String(16))
    occurred_at: Mapped[datetime]
