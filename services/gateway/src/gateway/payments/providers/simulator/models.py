from datetime import datetime

from sqlalchemy import BigInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import SimulatorBase


class SimulatedCharge(SimulatorBase):
    """The simulator's own charge ledger: one row per provider key, so a key is charged at most once."""

    __tablename__ = "charges"

    provider_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    card_token: Mapped[str] = mapped_column(String(40))
    result: Mapped[str] = mapped_column(String(16))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    provider_ref: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
