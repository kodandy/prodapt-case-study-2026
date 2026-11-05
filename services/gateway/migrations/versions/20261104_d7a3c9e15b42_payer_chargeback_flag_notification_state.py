"""payer, chargeback flag, notification read and response state

Revision ID: d7a3c9e15b42
Revises: c41e7a9d2b85
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d7a3c9e15b42"
down_revision: str | None = "c41e7a9d2b85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("payer_user_id", sa.Uuid(), nullable=True), schema="gateway")
    op.add_column(
        "payments",
        sa.Column("chargeback_flag", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        schema="gateway",
    )
    op.create_foreign_key(
        op.f("fk_payments_payer_user_id_users"),
        "payments",
        "users",
        ["payer_user_id"],
        ["id"],
        source_schema="gateway",
        referent_schema="gateway",
    )
    op.create_index(
        "ix_payments_payer_history",
        "payments",
        ["payer_user_id", "created_at", "id"],
        unique=False,
        schema="gateway",
    )
    op.add_column(
        "notifications", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True), schema="gateway"
    )
    op.add_column(
        "notifications", sa.Column("response", sa.String(length=16), nullable=True), schema="gateway"
    )
    op.add_column(
        "notifications",
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        schema="gateway",
    )
    op.create_index(
        "ix_notifications_recipient_feed",
        "notifications",
        ["recipient_type", "recipient_id", "created_at", "id"],
        unique=False,
        schema="gateway",
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_recipient_feed", table_name="notifications", schema="gateway")
    op.drop_column("notifications", "responded_at", schema="gateway")
    op.drop_column("notifications", "response", schema="gateway")
    op.drop_column("notifications", "read_at", schema="gateway")
    op.drop_index("ix_payments_payer_history", table_name="payments", schema="gateway")
    op.drop_constraint(
        op.f("fk_payments_payer_user_id_users"), "payments", schema="gateway", type_="foreignkey"
    )
    op.drop_column("payments", "chargeback_flag", schema="gateway")
    op.drop_column("payments", "payer_user_id", schema="gateway")
