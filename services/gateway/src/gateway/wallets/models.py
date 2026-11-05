import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk
from gateway.payments.fx import models as _fx_models  # noqa: F401  (FK target table)


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


class Transfer(GatewayBase):
    """P2P wallet transfer, same currency, settled synchronously (`succeeded` only in Basic)."""

    __tablename__ = "transfers"
    __table_args__ = (
        UniqueConstraint("sender_user_id", "idempotency_key"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
        Index("ix_transfers_recipient_created", "recipient_user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    sender_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    sender_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"))
    recipient_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    state: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]
    recipient_currency: Mapped[str | None] = mapped_column(String(3))
    """Cross-currency only (with the two columns below); NULL for a same-currency transfer."""
    recipient_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    fx_rate_id: Mapped[str | None] = mapped_column(ForeignKey("fx_rates.id"))


class WalletTopUp(GatewayBase):
    """Demo funding source credit (`POST /v1/me/top-ups`), idempotent per user."""

    __tablename__ = "wallet_top_ups"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime]


class Payout(GatewayBase):
    """External-rail payout (bank_transfer / upi). The raw destination is never stored, only its mask."""

    __tablename__ = "payouts"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key"),
        CheckConstraint("amount_minor > 0", name="amount_positive"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ledger_accounts.id"))
    method: Mapped[str] = mapped_column(String(16))
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    destination_masked: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    """pending | succeeded | failed | unknown"""
    failure_code: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
