"""add transfer facts

Revision ID: 7c2e9a4b1d05
Revises: 318aeacd647d
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7c2e9a4b1d05"
down_revision: str | None = "318aeacd647d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transfer_facts",
        sa.Column("transfer_id", sa.Uuid(), nullable=False),
        sa.Column("sender_user_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("country_sender", sa.String(length=2), nullable=False),
        sa.Column("country_receiver", sa.String(length=2), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("rule_id", sa.String(length=64), nullable=True),
        sa.Column("kyc_tier", sa.Integer(), nullable=True),
        sa.Column("limit_minor", sa.BigInteger(), nullable=True),
        sa.Column("limit_currency", sa.String(length=3), nullable=True),
        sa.Column("next_tier_limit_minor", sa.BigInteger(), nullable=True),
        sa.Column("sender_balance_minor", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingest_source", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("transfer_id", name=op.f("pk_transfer_facts")),
        schema="ops_ai",
    )
    op.create_index(
        op.f("ix_ops_ai_transfer_facts_sender_user_id"),
        "transfer_facts",
        ["sender_user_id"],
        unique=False,
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ops_ai_transfer_facts_sender_user_id"), table_name="transfer_facts", schema="ops_ai"
    )
    op.drop_table("transfer_facts", schema="ops_ai")
