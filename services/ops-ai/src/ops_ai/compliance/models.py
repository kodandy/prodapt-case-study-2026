import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class SubjectErasure(OpsAiBase):
    """One row per processed gateway erasure request. Holds no user id, so the record itself is not PII."""

    __tablename__ = "subject_erasures"
    __table_args__ = (CheckConstraint("rows_pseudonymised >= 0", name="rows_pseudonymised_non_negative"),)

    request_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    processed_at: Mapped[datetime]
    rows_pseudonymised: Mapped[int] = mapped_column(Integer)
