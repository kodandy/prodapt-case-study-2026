import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk
from gateway.payments.fx import models as _fx_models  # noqa: F401  (FK target table)


class JournalEntry(GatewayBase):
    __tablename__ = "journal_entries"

    id: Mapped[uuid.UUID] = uuid_pk()
    ref_type: Mapped[str] = mapped_column(String(32))
    ref_id: Mapped[uuid.UUID] = mapped_column(index=True)
    created_at: Mapped[datetime]
    fx_rate_id: Mapped[str | None] = mapped_column(ForeignKey("fx_rates.id"))
    """Set on a journal with legs in two currencies (the rate that converted them); NULL otherwise."""


class Posting(GatewayBase):
    """Signed amount: debit > 0, credit < 0. Each journal sums to zero per currency."""

    __tablename__ = "postings"

    id: Mapped[uuid.UUID] = uuid_pk()
    journal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"), index=True)
    currency: Mapped[str] = mapped_column(String(3))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
