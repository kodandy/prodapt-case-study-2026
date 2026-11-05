import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk


class KycReview(GatewayBase):
    """A KYC re-verification case (R2-10). At most one `open` review per user (partial unique index)."""

    __tablename__ = "kyc_reviews"
    __table_args__ = (
        CheckConstraint("status IN ('open', 'verified', 'rejected')", name="status_valid"),
        Index(
            "uq_kyc_reviews_open_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    reasons: Mapped[list[str]] = mapped_column(ARRAY(String(32)))
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    resolved_by: Mapped[str | None] = mapped_column(String(64))


class DataSubjectRequest(GatewayBase):
    """GDPR data-subject request with its statutory deadline (R2-10)."""

    __tablename__ = "data_subject_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('access', 'erasure', 'rectification')", name="kind_valid"),
        CheckConstraint("status IN ('received', 'completed', 'rejected')", name="status_valid"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    received_at: Mapped[datetime]
    due_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]
    detail: Mapped[dict[str, object]] = mapped_column(JSONB)
