"""add the kyc_review ticket source and ops_ai.subject_erasures (R2-10)

Revision ID: c7d3e9a1f254
Revises: b12c4e7a9f31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7d3e9a1f254"
down_revision: str | None = "b12c4e7a9f31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_SOURCES = "source IN ('card_testing', 'assistant_escalation', 'dispute', 'unknown_payment')"
NEW_SOURCES = "source IN ('card_testing', 'assistant_escalation', 'dispute', 'unknown_payment', 'kyc_review')"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_tickets_source"), "tickets", type_="check", schema="ops_ai")
    op.create_check_constraint(op.f("ck_tickets_source"), "tickets", NEW_SOURCES, schema="ops_ai")
    op.create_table(
        "subject_erasures",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rows_pseudonymised", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "rows_pseudonymised >= 0", name=op.f("ck_subject_erasures_rows_pseudonymised_non_negative")
        ),
        sa.PrimaryKeyConstraint("request_id", name=op.f("pk_subject_erasures")),
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_table("subject_erasures", schema="ops_ai")
    op.execute("DELETE FROM ops_ai.tickets WHERE source = 'kyc_review'")
    op.drop_constraint(op.f("ck_tickets_source"), "tickets", type_="check", schema="ops_ai")
    op.create_check_constraint(op.f("ck_tickets_source"), "tickets", OLD_SOURCES, schema="ops_ai")
