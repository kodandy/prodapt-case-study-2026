"""login throttle

Revision ID: c41e7a9d2b85
Revises: 927e5613d635
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c41e7a9d2b85"
down_revision: str | None = "927e5613d635"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_throttle",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "failed_attempts >= 0", name=op.f("ck_login_throttle_failed_attempts_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["gateway.users.id"], name=op.f("fk_login_throttle_user_id_users")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_login_throttle")),
        schema="gateway",
    )


def downgrade() -> None:
    op.drop_table("login_throttle", schema="gateway")
