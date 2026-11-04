"""add merchant_memory (B10.3 long-term memory)

Revision ID: b10a4e7c2d91
Revises: b6e1c2d3f4a5
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b10a4e7c2d91"
down_revision: str | None = "b6e1c2d3f4a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "merchant_memory",
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("merchant_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_session_id", sa.Uuid(), nullable=False),
        sa.Column("source_turn_id", sa.Uuid(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.String(length=128), nullable=True),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 500", name=op.f("ck_merchant_memory_content_length")
        ),
        sa.PrimaryKeyConstraint("memory_id", name=op.f("pk_merchant_memory")),
        schema="ops_ai",
    )
    op.create_index(
        "ix_merchant_memory_merchant_created",
        "merchant_memory",
        ["merchant_id", "created_at"],
        unique=False,
        schema="ops_ai",
    )
    op.create_index(
        "uq_merchant_memory_source_turn",
        "merchant_memory",
        ["merchant_id", "source_turn_id"],
        unique=True,
        schema="ops_ai",
        postgresql_where=sa.text("supersedes_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_merchant_memory_source_turn", table_name="merchant_memory", schema="ops_ai")
    op.drop_index("ix_merchant_memory_merchant_created", table_name="merchant_memory", schema="ops_ai")
    op.drop_table("merchant_memory", schema="ops_ai")
