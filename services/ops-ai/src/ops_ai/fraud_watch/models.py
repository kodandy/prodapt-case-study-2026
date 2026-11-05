import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, Text
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


class FraudAlertRuleVersion(OpsAiBase):
    """The merchant rule version the live watcher applied when it last raised or refreshed the alert."""

    __tablename__ = "fraud_alert_rule_versions"
    __table_args__ = (CheckConstraint("rule_version >= 0", name="rule_version"),)

    alert_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ops_ai.fraud_alerts.alert_id", ondelete="CASCADE"), primary_key=True
    )
    merchant_id: Mapped[uuid.UUID]
    rule_version: Mapped[int] = mapped_column(Integer)
    applied_at: Mapped[datetime]
