"""add agent_messages, kyc_profiles, feedback, fraud_rule_versions, incident_feedback_scores (R2)

Revision ID: c7e3a1d9b4f2
Revises: c7d3e9a1f254
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7e3a1d9b4f2"
down_revision: str | None = "c7d3e9a1f254"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_messages",
        sa.Column("message_id", sa.Uuid(), nullable=False),
        sa.Column("correlation_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("sender", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=48), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("message_id", name=op.f("pk_agent_messages")),
        sa.UniqueConstraint(
            "correlation_id", "sequence", name=op.f("uq_agent_messages_correlation_id_sequence")
        ),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_ai_agent_messages_correlation_id"),
        "agent_messages",
        ["correlation_id"],
        unique=False,
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_ai_agent_messages_merchant_id"),
        "agent_messages",
        ["merchant_id"],
        unique=False,
        schema="ops_ai",
    )
    op.create_table(
        "kyc_profiles",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kyc_status", sa.String(length=16), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=False),
        sa.Column("ingest_source", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "kyc_status IN ('verified', 'pending', 'failed')", name=op.f("ck_kyc_profiles_kyc_status")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_kyc_profiles")),
        schema="ops_ai",
    )
    op.create_table(
        "feedback",
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.String(length=128), nullable=False),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "target_kind IN ('alert', 'answer', 'incident')", name=op.f("ck_feedback_target_kind")
        ),
        sa.CheckConstraint(
            "verdict IN ('true_positive', 'false_positive', 'helpful', 'not_helpful')",
            name=op.f("ck_feedback_verdict"),
        ),
        sa.PrimaryKeyConstraint("feedback_id", name=op.f("pk_feedback")),
        sa.UniqueConstraint(
            "merchant_id",
            "target_kind",
            "target_id",
            name=op.f("uq_feedback_merchant_id_target_kind_target_id"),
        ),
        schema="ops_ai",
    )
    op.create_table(
        "fraud_rule_versions",
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("steps", sa.Integer(), nullable=False),
        sa.Column("min_micro_payments", sa.Integer(), nullable=False),
        sa.Column("min_micro_share", sa.Float(), nullable=False),
        sa.Column("min_distinct_cards", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("feedback_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("steps BETWEEN 0 AND 5", name=op.f("ck_fraud_rule_versions_steps")),
        sa.PrimaryKeyConstraint("merchant_id", "version", name=op.f("pk_fraud_rule_versions")),
        schema="ops_ai",
    )
    op.create_table(
        "incident_feedback_scores",
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("incident_id", sa.String(length=32), nullable=False),
        sa.Column("helpful", sa.Integer(), server_default="0", nullable=False),
        sa.Column("not_helpful", sa.Integer(), server_default="0", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("merchant_id", "incident_id", name=op.f("pk_incident_feedback_scores")),
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_table("incident_feedback_scores", schema="ops_ai")
    op.drop_table("fraud_rule_versions", schema="ops_ai")
    op.drop_table("feedback", schema="ops_ai")
    op.drop_table("kyc_profiles", schema="ops_ai")
    op.drop_index(op.f("ix_ops_ai_agent_messages_merchant_id"), table_name="agent_messages", schema="ops_ai")
    op.drop_index(
        op.f("ix_ops_ai_agent_messages_correlation_id"), table_name="agent_messages", schema="ops_ai"
    )
    op.drop_table("agent_messages", schema="ops_ai")
