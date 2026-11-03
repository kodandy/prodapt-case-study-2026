import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class TransferFact(OpsAiBase):
    """The external transfer system's decision record, one row per p2p transfer.

    `rule_id` is kept for audit only and never leaves ops-ai.
    """

    __tablename__ = "transfer_facts"

    transfer_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    sender_user_id: Mapped[uuid.UUID] = mapped_column(index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    country_sender: Mapped[str] = mapped_column(String(2))
    country_receiver: Mapped[str] = mapped_column(String(2))
    reason_code: Mapped[str | None] = mapped_column(String(64))
    rule_id: Mapped[str | None] = mapped_column(String(64))
    kyc_tier: Mapped[int | None] = mapped_column(Integer)
    limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    limit_currency: Mapped[str | None] = mapped_column(String(3))
    next_tier_limit_minor: Mapped[int | None] = mapped_column(BigInteger)
    sender_balance_minor: Mapped[int | None] = mapped_column(BigInteger)
    decided_at: Mapped[datetime]
    ingest_source: Mapped[str] = mapped_column(String(16))
