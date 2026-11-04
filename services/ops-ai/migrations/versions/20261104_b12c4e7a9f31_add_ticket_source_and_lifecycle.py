"""add tickets.source, tickets.assignee and tickets.updated_at (B12 ticket sources and lifecycle)

Revision ID: b12c4e7a9f31
Revises: b10a4e7c2d91
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b12c4e7a9f31"
down_revision: str | None = "b10a4e7c2d91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tickets",
        sa.Column("source", sa.String(length=32), server_default="card_testing", nullable=False),
        schema="ops_ai",
    )
    op.add_column("tickets", sa.Column("assignee", sa.String(length=64), nullable=True), schema="ops_ai")
    op.add_column(
        "tickets", sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True), schema="ops_ai"
    )
    op.create_check_constraint(
        op.f("ck_tickets_source"),
        "tickets",
        "source IN ('card_testing', 'assistant_escalation', 'dispute', 'unknown_payment')",
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tickets_source"), "tickets", type_="check", schema="ops_ai")
    op.drop_column("tickets", "updated_at", schema="ops_ai")
    op.drop_column("tickets", "assignee", schema="ops_ai")
    op.drop_column("tickets", "source", schema="ops_ai")
