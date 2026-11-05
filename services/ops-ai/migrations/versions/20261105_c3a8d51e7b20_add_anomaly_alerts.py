"""add anomaly_alerts (R2-8 real-time anomaly alerts)

Revision ID: c3a8d51e7b20
Revises: b12c4e7a9f31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3a8d51e7b20"
down_revision: str | None = "b12c4e7a9f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "anomaly_alerts",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("alert_key", sa.Text(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("metric", sa.String(length=16), nullable=False),
        sa.Column("detail", sa.String(length=64), nullable=True),
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed", sa.Double(), nullable=False),
        sa.Column("expected", sa.Double(), nullable=False),
        sa.Column("zscore", sa.Double(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("alert_id", name=op.f("pk_anomaly_alerts")),
        sa.UniqueConstraint("alert_key", name=op.f("uq_anomaly_alerts_alert_key")),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_ai_anomaly_alerts_merchant_id"),
        "anomaly_alerts",
        ["merchant_id"],
        unique=False,
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ops_ai_anomaly_alerts_merchant_id"), table_name="anomaly_alerts", schema="ops_ai")
    op.drop_table("anomaly_alerts", schema="ops_ai")
