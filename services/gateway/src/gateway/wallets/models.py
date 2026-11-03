import uuid

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk


class LedgerAccount(GatewayBase):
    """owner_type: user | merchant | platform. Platform accounts have owner_id NULL."""

    __tablename__ = "ledger_accounts"
    __table_args__ = (
        UniqueConstraint("owner_type", "owner_id", "kind", "currency", postgresql_nulls_not_distinct=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    owner_type: Mapped[str] = mapped_column(String(16))
    owner_id: Mapped[uuid.UUID | None]
    kind: Mapped[str] = mapped_column(String(32))
    currency: Mapped[str] = mapped_column(String(3))


class AccountBalance(GatewayBase):
    """Synchronous balance row; user wallets only (platform accounts are append-only postings)."""

    __tablename__ = "account_balances"
    __table_args__ = (
        CheckConstraint("available_minor >= 0", name="available_non_negative"),
        CheckConstraint("held_minor >= 0", name="held_non_negative"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"), primary_key=True)
    available_minor: Mapped[int] = mapped_column(BigInteger, default=0)
    held_minor: Mapped[int] = mapped_column(BigInteger, default=0)
