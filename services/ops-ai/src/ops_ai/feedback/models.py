import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from ops_ai.db import OpsAiBase
from ops_ai.scope import MERCHANT_ROLES

TARGET_KINDS = ("alert", "answer", "incident")
VERDICTS = ("true_positive", "false_positive", "helpful", "not_helpful")


def _one_of(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class Feedback(OpsAiBase):
    """One ops verdict per (merchant, target): a merchant votes once on each alert, answer or incident."""

    __tablename__ = "feedback"
    __table_args__ = (
        CheckConstraint(_one_of("target_kind", TARGET_KINDS), name="target_kind"),
        CheckConstraint(_one_of("verdict", VERDICTS), name="verdict"),
        UniqueConstraint("merchant_id", "target_kind", "target_id"),
    )

    feedback_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID]
    target_kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(128))
    verdict: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class FraudRuleVersion(OpsAiBase):
    """Append-only per-merchant card-testing threshold versions; version 0 is the shipped baseline
    (no row)."""

    __tablename__ = "fraud_rule_versions"
    __table_args__ = (CheckConstraint("steps BETWEEN 0 AND 5", name="steps"),)

    merchant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    steps: Mapped[int] = mapped_column(Integer)
    min_micro_payments: Mapped[int] = mapped_column(Integer)
    min_micro_share: Mapped[float] = mapped_column(Float)
    min_distinct_cards: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(64))
    feedback_id: Mapped[uuid.UUID | None]
    created_at: Mapped[datetime]


class FraudRuleVersionActor(OpsAiBase):
    """Who wrote a `fraud_rule_versions` row: the token principal of the feedback or revert behind it."""

    __tablename__ = "fraud_rule_version_actors"
    __table_args__ = (
        ForeignKeyConstraint(
            ["merchant_id", "version"],
            ["ops_ai.fraud_rule_versions.merchant_id", "ops_ai.fraud_rule_versions.version"],
            ondelete="CASCADE",
        ),
        CheckConstraint(_one_of("actor_role", MERCHANT_ROLES), name="actor_role"),
    )

    merchant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    actor: Mapped[str] = mapped_column(String(64))
    actor_role: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime]


class IncidentFeedbackScore(OpsAiBase):
    """Per-merchant helpful / not-helpful counts of a historical incident, used by reranking."""

    __tablename__ = "incident_feedback_scores"

    merchant_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    incident_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    helpful: Mapped[int] = mapped_column(Integer, server_default="0")
    not_helpful: Mapped[int] = mapped_column(Integer, server_default="0")
    updated_at: Mapped[datetime]
