from datetime import datetime

from sqlalchemy import SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase


class CardToken(GatewayBase):
    """The PAN is never stored: only token, BIN, last4 and a keyed fingerprint."""

    __tablename__ = "card_tokens"

    token: Mapped[str] = mapped_column(String(40), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    bin: Mapped[str] = mapped_column(String(8))
    last4: Mapped[str] = mapped_column(String(4))
    exp_month: Mapped[int] = mapped_column(SmallInteger)
    exp_year: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime]
