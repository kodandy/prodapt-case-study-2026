"""add ops_alerts and ops_alert_deliveries (B11.2 ops staff alerts)

Revision ID: c4f1a8e2b7d3
Revises: b12c4e7a9f31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4f1a8e2b7d3"
down_revision: str | None = "b12c4e7a9f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ops_alerts",
        sa.Column("alert_id", sa.Uuid(), primary_key=True),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("fraud_alert_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.Uuid(), nullable=True),
        sa.UniqueConstraint("source_key", name=op.f("uq_ops_alerts_source_key")),
        sa.CheckConstraint("severity IN ('high', 'medium', 'low')", name=op.f("ck_ops_alerts_severity")),
        schema="ops_ai",
    )
    op.create_index(op.f("ix_ops_alerts_merchant_id"), "ops_alerts", ["merchant_id"], schema="ops_ai")
    op.create_index(op.f("ix_ops_alerts_created_at"), "ops_alerts", ["created_at"], schema="ops_ai")
    op.create_table(
        "ops_alert_deliveries",
        sa.Column("delivery_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "alert_id",
            sa.Uuid(),
            sa.ForeignKey("ops_ai.ops_alerts.alert_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stream_entry_id", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "alert_id", "channel", name=op.f("uq_ops_alert_deliveries_alert_id_channel")
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed')", name=op.f("ck_ops_alert_deliveries_status")
        ),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_alert_deliveries_status_created_at"),
        "ops_alert_deliveries",
        ["status", "created_at"],
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ops_alert_deliveries_status_created_at"),
        table_name="ops_alert_deliveries",
        schema="ops_ai",
    )
    op.drop_table("ops_alert_deliveries", schema="ops_ai")
    op.drop_index(op.f("ix_ops_alerts_created_at"), table_name="ops_alerts", schema="ops_ai")
    op.drop_index(op.f("ix_ops_alerts_merchant_id"), table_name="ops_alerts", schema="ops_ai")
    op.drop_table("ops_alerts", schema="ops_ai")
