"""add actor_kind / actor_id to fraud_rule_version_actors (R2-7 per-actor id)

Revision ID: f3c8a1d6e9b4
Revises: f3c8a2d6b915
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c8a1d6e9b4"
down_revision: str | None = "f3c8a2d6b915"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fraud_rule_version_actors",
        sa.Column("actor_kind", sa.String(length=32), server_default="merchant_principal", nullable=False),
        schema="ops_ai",
    )
    op.add_column(
        "fraud_rule_version_actors", sa.Column("actor_id", sa.Uuid(), nullable=True), schema="ops_ai"
    )
    op.create_check_constraint(
        op.f("ck_fraud_rule_version_actors_actor_kind"),
        "fraud_rule_version_actors",
        "actor_kind IN ('api_key', 'merchant_principal')",
        schema="ops_ai",
    )
    op.create_check_constraint(
        op.f("ck_fraud_rule_version_actors_actor_id_matches_kind"),
        "fraud_rule_version_actors",
        "(actor_kind = 'api_key') = (actor_id IS NOT NULL)",
        schema="ops_ai",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_fraud_rule_version_actors_actor_id_matches_kind"),
        "fraud_rule_version_actors",
        type_="check",
        schema="ops_ai",
    )
    op.drop_constraint(
        op.f("ck_fraud_rule_version_actors_actor_kind"),
        "fraud_rule_version_actors",
        type_="check",
        schema="ops_ai",
    )
    op.drop_column("fraud_rule_version_actors", "actor_id", schema="ops_ai")
    op.drop_column("fraud_rule_version_actors", "actor_kind", schema="ops_ai")
