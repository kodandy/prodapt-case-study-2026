from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase


class FxRate(GatewayBase):
    """`1 base = rate quote`. The rate in force at `now` is the row with the latest `valid_from <= now`."""

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint("base_currency", "quote_currency", "valid_from"),
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3))
    quote_currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[Decimal] = mapped_column(Numeric(20, 10))
    valid_from: Mapped[datetime]
