import uuid
from datetime import datetime

from sqlalchemy import Double, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase


class AnomalyAlert(OpsAiBase):
    """One row per (metric, merchant, bucket, detail); a repeat in the same bucket updates it in place."""

    __tablename__ = "anomaly_alerts"

    alert_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    alert_key: Mapped[str] = mapped_column(Text, unique=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    metric: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(String(64))
    bucket_start: Mapped[datetime]
    observed: Mapped[float] = mapped_column(Double)
    expected: Mapped[float] = mapped_column(Double)
    zscore: Mapped[float] = mapped_column(Double)
    attempts: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
