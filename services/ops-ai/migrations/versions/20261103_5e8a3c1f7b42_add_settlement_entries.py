"""add settlement entries

Revision ID: 5e8a3c1f7b42
Revises: 9d4f1b7e2c63
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e8a3c1f7b42"
down_revision: str | None = "9d4f1b7e2c63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "settlement_entries",
        sa.Column("payment_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("statement_period", sa.String(length=7), nullable=False),
        sa.Column("refunded", sa.Boolean(), nullable=False),
        sa.Column("chargeback", sa.Boolean(), nullable=False),
        sa.Column("ingest_source", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("payment_id", name=op.f("pk_settlement_entries")),
        schema="ops_ai",
    )
    op.create_index(
        "ix_ops_ai_settlement_entries_merchant_period",
        "settlement_entries",
        ["merchant_id", "statement_period"],
        unique=False,
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_ops_ai_settlement_entries_merchant_period", table_name="settlement_entries", schema="ops_ai"
    )
    op.drop_table("settlement_entries", schema="ops_ai")
