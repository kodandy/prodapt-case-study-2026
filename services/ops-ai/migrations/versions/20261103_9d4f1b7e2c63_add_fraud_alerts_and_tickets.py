"""add payment_facts.card_ref and its fraud index, fraud alerts and tickets

Revision ID: 9d4f1b7e2c63
Revises: 7c2e9a4b1d05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9d4f1b7e2c63"
down_revision: str | None = "7c2e9a4b1d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payment_facts", sa.Column("card_ref", sa.String(length=32), nullable=True), schema="ops_ai"
    )
    op.create_index(
        "ix_ops_ai_payment_facts_merchant_method_occurred_at",
        "payment_facts",
        ["merchant_id", "method", "occurred_at"],
        unique=False,
        schema="ops_ai",
    )
    op.create_table(
        "fraud_alerts",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("window_key", sa.Text(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("first_payment_id", sa.Uuid(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payment_count", sa.Integer(), nullable=False),
        sa.Column("features", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("alert_id", name=op.f("pk_fraud_alerts")),
        sa.UniqueConstraint("window_key", name=op.f("uq_fraud_alerts_window_key")),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_ai_fraud_alerts_merchant_id"),
        "fraud_alerts",
        ["merchant_id"],
        unique=False,
        schema="ops_ai",
    )
    op.create_table(
        "tickets",
        sa.Column("ticket_id", sa.Uuid(), nullable=False),
        sa.Column("source_key", sa.Text(), nullable=False),
        sa.Column("queue", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="open", nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("alert_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "queue IN ('fraud', 'payments_ops', 'settlement', 'compliance')", name=op.f("ck_tickets_queue")
        ),
        sa.CheckConstraint("priority IN ('high', 'normal', 'low')", name=op.f("ck_tickets_priority")),
        sa.CheckConstraint("status IN ('open', 'assigned', 'resolved')", name=op.f("ck_tickets_status")),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["ops_ai.fraud_alerts.alert_id"],
            name=op.f("fk_tickets_alert_id_fraud_alerts"),
        ),
        sa.PrimaryKeyConstraint("ticket_id", name=op.f("pk_tickets")),
        sa.UniqueConstraint("source_key", name=op.f("uq_tickets_source_key")),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_ai_tickets_merchant_id"), "tickets", ["merchant_id"], unique=False, schema="ops_ai"
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ops_ai_tickets_merchant_id"), table_name="tickets", schema="ops_ai")
    op.drop_table("tickets", schema="ops_ai")
    op.drop_index(op.f("ix_ops_ai_fraud_alerts_merchant_id"), table_name="fraud_alerts", schema="ops_ai")
    op.drop_table("fraud_alerts", schema="ops_ai")
    op.drop_index(
        "ix_ops_ai_payment_facts_merchant_method_occurred_at", table_name="payment_facts", schema="ops_ai"
    )
    op.drop_column("payment_facts", "card_ref", schema="ops_ai")
