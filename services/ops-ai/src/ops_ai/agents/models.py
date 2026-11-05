import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase

KYC_STATUSES = ("verified", "pending", "failed")


class AgentMessageRecord(OpsAiBase):
    """Append-only audit trail of every agent-to-agent message (one correlation per investigation)."""

    __tablename__ = "agent_messages"
    __table_args__ = (UniqueConstraint("correlation_id", "sequence"),)

    message_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    correlation_id: Mapped[uuid.UUID] = mapped_column(index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    sender: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(32))
    intent: Mapped[str] = mapped_column(String(48))
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONB)
    merchant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    user_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]


class KycProfile(OpsAiBase):
    """KYC facts of a P2P sender, read by the compliance agent."""

    __tablename__ = "kyc_profiles"
    __table_args__ = (
        CheckConstraint(
            f"kyc_status IN ({', '.join(repr(status) for status in KYC_STATUSES)})", name="kyc_status"
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    kyc_status: Mapped[str] = mapped_column(String(16))
    country: Mapped[str] = mapped_column(String(2))
    ingest_source: Mapped[str] = mapped_column(String(16))
