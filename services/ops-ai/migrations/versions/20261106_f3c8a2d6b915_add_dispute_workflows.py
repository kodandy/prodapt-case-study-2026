"""add dispute_workflows / dispute_workflow_steps (gw.disputes.v1 projection, R2-1 / R2-2)

Revision ID: f3c8a2d6b915
Revises: e5a9c3f17b28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c8a2d6b915"
down_revision: str | None = "e5a9c3f17b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispute_workflows",
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("network_reason_code", sa.String(length=16), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("payment_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("refunded_minor", sa.BigInteger(), nullable=False),
        sa.Column("dispute_amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("chargeback_minor", sa.BigInteger(), nullable=False),
        sa.Column("chargeback_fee_minor", sa.BigInteger(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("dispute_id", name=op.f("pk_dispute_workflows")),
        schema="ops_ai",
    )
    op.create_index(
        "ix_ops_ai_dispute_workflows_merchant_updated_at",
        "dispute_workflows",
        ["merchant_id", "updated_at"],
        schema="ops_ai",
    )
    op.create_table(
        "dispute_workflow_steps",
        sa.Column("dispute_id", sa.Uuid(), nullable=False),
        sa.Column("entity_version", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("from_state", sa.String(length=16), nullable=True),
        sa.Column("to_state", sa.String(length=16), nullable=False),
        sa.Column("actor_role", sa.String(length=16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["ops_ai.dispute_workflows.dispute_id"],
            name=op.f("fk_dispute_workflow_steps_dispute_id_dispute_workflows"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("dispute_id", "entity_version", name=op.f("pk_dispute_workflow_steps")),
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_table("dispute_workflow_steps", schema="ops_ai")
    op.drop_index(
        "ix_ops_ai_dispute_workflows_merchant_updated_at", table_name="dispute_workflows", schema="ops_ai"
    )
    op.drop_table("dispute_workflows", schema="ops_ai")
    # Without its dedupe rows a replay after re-upgrading rebuilds the projection instead of skipping it.
    op.execute("DELETE FROM ops_ai.processed_events WHERE consumer = 'ops-ai.dispute-workflows'")
