"""add fraud_rule_version_actors and fraud_alert_rule_versions (R2-7 live thresholds and rule actors)

Revision ID: d2b7e4a9c1f6
Revises: c4f1a8e2b7d3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2b7e4a9c1f6"
down_revision: str | None = "c4f1a8e2b7d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fraud_rule_version_actors",
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column("actor_role", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "actor_role IN ('merchant_admin', 'merchant_support')",
            name=op.f("ck_fraud_rule_version_actors_actor_role"),
        ),
        sa.ForeignKeyConstraint(
            ["merchant_id", "version"],
            ["ops_ai.fraud_rule_versions.merchant_id", "ops_ai.fraud_rule_versions.version"],
            name=op.f("fk_fraud_rule_version_actors_merchant_id_fraud_rule_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("merchant_id", "version", name=op.f("pk_fraud_rule_version_actors")),
        schema="ops_ai",
    )
    op.create_table(
        "fraud_alert_rule_versions",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version", sa.Integer(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rule_version >= 0", name=op.f("ck_fraud_alert_rule_versions_rule_version")),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["ops_ai.fraud_alerts.alert_id"],
            name=op.f("fk_fraud_alert_rule_versions_alert_id_fraud_alerts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("alert_id", name=op.f("pk_fraud_alert_rule_versions")),
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_table("fraud_alert_rule_versions", schema="ops_ai")
    op.drop_table("fraud_rule_version_actors", schema="ops_ai")
