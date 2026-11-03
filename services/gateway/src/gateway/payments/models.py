import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk


class Payment(GatewayBase):
    """state: created | succeeded | declined | failed | unknown."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "refunded_minor >= 0 AND disputed_minor >= 0 AND chargeback_minor >= 0",
            name="totals_non_negative",
        ),
        CheckConstraint(
            "refunded_minor + disputed_minor + chargeback_minor <= amount_minor", name="totals_within_amount"
        ),
        Index("ix_payments_merchant_history", "merchant_id", "created_at", "id"),
        Index("ix_payments_card_fingerprint_created", "card_fingerprint", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    card_token: Mapped[str | None] = mapped_column(ForeignKey("card_tokens.token"))
    card_fingerprint: Mapped[str | None] = mapped_column(String(64))
    """Null only for payments created before risk scoring."""
    decline_code: Mapped[str | None] = mapped_column(String(64))
    entity_version: Mapped[int] = mapped_column(Integer, default=1)
    refunded_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    disputed_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    """Reserved by a dispute in opened | under_review."""
    chargeback_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    """Null only for payments created before risk scoring."""
    risk_reasons: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    risk_flagged: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    risk_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PaymentAttempt(GatewayBase):
    """The attempt id is the provider idempotency key."""

    __tablename__ = "payment_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    route_config_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    provider_ref: Mapped[str | None] = mapped_column(String(64))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Refund(GatewayBase):
    """Synchronous in Thin: recorded `succeeded` with its reversal journal."""

    __tablename__ = "refunds"
    __table_args__ = (UniqueConstraint("merchant_id", "idempotency_key"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]


class Dispute(GatewayBase):
    """state: opened | under_review | won | lost."""

    __tablename__ = "disputes"
    __table_args__ = (
        Index(
            "uq_disputes_one_active_per_payment",
            "payment_id",
            unique=True,
            postgresql_where=text("state IN ('opened', 'under_review')"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    reason: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    evidence_text: Mapped[str | None] = mapped_column(Text)
    chargeback_fee_minor: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
