import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql.elements import conv

from ops_ai.db import OpsAiBase


class SettlementEntry(OpsAiBase):
    """One captured merchant card payment as the acquirer's settlement report lists it.

    `payment_id` is the same id as `payment_facts.payment_id`; `statement_period` (YYYY-MM) is the report's
    label, not derived from `captured_at`.
    """

    __tablename__ = "settlement_entries"
    __table_args__ = (
        Index("ix_ops_ai_settlement_entries_merchant_period", "merchant_id", "statement_period"),
        CheckConstraint("amount_minor > 0", name=conv("ck_settlement_entries_amount_positive")),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name=conv("ck_settlement_entries_currency_format")),
        CheckConstraint(
            "statement_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name=conv("ck_settlement_entries_period_format"),
        ),
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID]
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    captured_at: Mapped[datetime]
    statement_period: Mapped[str] = mapped_column(String(7))
    refunded: Mapped[bool]
    chargeback: Mapped[bool]
    ingest_source: Mapped[str] = mapped_column(String(16))
