"""add alert_notifications (SQ3 outbound fraud alert notification outbox)

Revision ID: e5a9c3f17b28
Revises: d2b7e4a9c1f6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e5a9c3f17b28"
down_revision: str | None = "d2b7e4a9c1f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "alert_notifications",
        sa.Column("notification_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "fraud_alert_id",
            sa.Uuid(),
            sa.ForeignKey("ops_ai.fraud_alerts.alert_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("fraud_alert_id", name=op.f("uq_alert_notifications_fraud_alert_id")),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')", name=op.f("ck_alert_notifications_status")
        ),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_alert_notifications_status_created_at"),
        "alert_notifications",
        ["status", "created_at"],
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_alert_notifications_status_created_at"), table_name="alert_notifications", schema="ops_ai"
    )
    op.drop_table("alert_notifications", schema="ops_ai")
