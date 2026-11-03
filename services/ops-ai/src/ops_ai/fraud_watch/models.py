import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class FraudAlert(OpsAiBase):
    """One alert per episode (`window_key`). `features` holds counts, ratios, BINs, currencies and reasons:
    never payment ids or card refs."""

    __tablename__ = "fraud_alerts"

    alert_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    window_key: Mapped[str] = mapped_column(Text, unique=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(32))
    first_payment_id: Mapped[uuid.UUID]
    started_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    payment_count: Mapped[int] = mapped_column(Integer)
    features: Mapped[dict[str, object]] = mapped_column(JSONB)
    created_at: Mapped[datetime]
