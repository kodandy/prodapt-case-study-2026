"""add settlement entry checks

Revision ID: a3f7c9e15b28
Revises: 5e8a3c1f7b42
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3f7c9e15b28"
down_revision: str | None = "5e8a3c1f7b42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECKS = (
    ("ck_settlement_entries_amount_positive", "amount_minor > 0"),
    ("ck_settlement_entries_currency_format", "currency ~ '^[A-Z]{3}$'"),
    ("ck_settlement_entries_period_format", "statement_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'"),
)


def upgrade() -> None:
    for name, condition in CHECKS:
        op.create_check_constraint(op.f(name), "settlement_entries", condition, schema="ops_ai")


def downgrade() -> None:
    for name, _condition in reversed(CHECKS):
        op.drop_constraint(op.f(name), "settlement_entries", type_="check", schema="ops_ai")
