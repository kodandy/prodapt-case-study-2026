"""rbac, ops users, gateway_app role, kyc and gdpr

Revision ID: 5d2e8b1c4a77
Revises: c41e7a9d2b85
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5d2e8b1c4a77"
down_revision: str | None = "c41e7a9d2b85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "gateway_app"
APP_SCHEMAS = ("gateway", "simulator")

CREATE_APP_ROLE = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gateway_app') THEN
    CREATE ROLE gateway_app NOLOGIN;
  END IF;
END
$$;
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(length=200), nullable=True), schema="gateway")
    op.add_column("users", sa.Column("country", sa.String(length=2), nullable=True), schema="gateway")
    op.add_column(
        "users",
        sa.Column("kyc_status", sa.String(length=16), server_default=sa.text("'unverified'"), nullable=False),
        schema="gateway",
    )
    op.add_column(
        "users", sa.Column("kyc_verified_at", sa.DateTime(timezone=True), nullable=True), schema="gateway"
    )
    op.add_column(
        "users", sa.Column("kyc_expires_at", sa.DateTime(timezone=True), nullable=True), schema="gateway"
    )
    op.add_column(
        "users", sa.Column("erased_at", sa.DateTime(timezone=True), nullable=True), schema="gateway"
    )
    op.create_check_constraint(
        op.f("ck_users_kyc_status_valid"),
        "users",
        "kyc_status IN ('unverified', 'verified', 'pending', 'rejected')",
        schema="gateway",
    )
    op.create_check_constraint(
        op.f("ck_users_country_alpha2"), "users", "country ~ '^[A-Z]{2}$'", schema="gateway"
    )

    op.add_column(
        "api_keys",
        sa.Column("role", sa.String(length=32), server_default=sa.text("'merchant_admin'"), nullable=False),
        schema="gateway",
    )
    op.create_check_constraint(
        op.f("ck_api_keys_role_valid"),
        "api_keys",
        "role IN ('merchant_admin', 'merchant_support')",
        schema="gateway",
    )

    op.create_table(
        "ops_users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("totp_secret_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("totp_last_used_step", sa.BigInteger(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('ops_agent', 'ops_lead')", name=op.f("ck_ops_users_role_valid")),
        sa.CheckConstraint("failed_attempts >= 0", name=op.f("ck_ops_users_failed_attempts_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ops_users")),
        sa.UniqueConstraint("email", name=op.f("uq_ops_users_email")),
        schema="gateway",
    )

    op.create_table(
        "kyc_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reasons", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('open', 'verified', 'rejected')", name=op.f("ck_kyc_reviews_status_valid")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["gateway.users.id"], name=op.f("fk_kyc_reviews_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_kyc_reviews")),
        schema="gateway",
    )
    op.create_index(op.f("ix_gateway_kyc_reviews_user_id"), "kyc_reviews", ["user_id"], schema="gateway")
    op.create_index(
        "uq_kyc_reviews_open_per_user",
        "kyc_reviews",
        ["user_id"],
        unique=True,
        schema="gateway",
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "data_subject_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.CheckConstraint(
            "kind IN ('access', 'erasure', 'rectification')", name=op.f("ck_data_subject_requests_kind_valid")
        ),
        sa.CheckConstraint(
            "status IN ('received', 'completed', 'rejected')",
            name=op.f("ck_data_subject_requests_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["gateway.users.id"], name=op.f("fk_data_subject_requests_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_subject_requests")),
        schema="gateway",
    )
    op.create_index(
        op.f("ix_gateway_data_subject_requests_user_id"),
        "data_subject_requests",
        ["user_id"],
        schema="gateway",
    )

    _grant_app_role()


def _grant_app_role() -> None:
    """P0-10: the app runs as `gateway_app` (SET ROLE), which may only SELECT/INSERT the audit log. The role
    is cluster-wide, so it is created idempotently and never dropped by a downgrade."""
    op.execute(CREATE_APP_ROLE)
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")
    for schema in APP_SCHEMAS:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {APP_ROLE}")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {schema} TO {APP_ROLE}")
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {schema} TO {APP_ROLE}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
        )
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
        )
    op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON gateway.audit_log FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON gateway.alembic_version FROM {APP_ROLE}")


def _revoke_app_role() -> None:
    for schema in APP_SCHEMAS:
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM {APP_ROLE}")
        op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON SEQUENCES FROM {APP_ROLE}")
        op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM {APP_ROLE}")
        op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {schema} FROM {APP_ROLE}")
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {APP_ROLE}")


def downgrade() -> None:
    _revoke_app_role()
    op.drop_index(
        op.f("ix_gateway_data_subject_requests_user_id"), table_name="data_subject_requests", schema="gateway"
    )
    op.drop_table("data_subject_requests", schema="gateway")
    op.drop_index("uq_kyc_reviews_open_per_user", table_name="kyc_reviews", schema="gateway")
    op.drop_index(op.f("ix_gateway_kyc_reviews_user_id"), table_name="kyc_reviews", schema="gateway")
    op.drop_table("kyc_reviews", schema="gateway")
    op.drop_table("ops_users", schema="gateway")
    op.drop_constraint(op.f("ck_api_keys_role_valid"), "api_keys", schema="gateway", type_="check")
    op.drop_column("api_keys", "role", schema="gateway")
    op.drop_constraint(op.f("ck_users_country_alpha2"), "users", schema="gateway", type_="check")
    op.drop_constraint(op.f("ck_users_kyc_status_valid"), "users", schema="gateway", type_="check")
    for column in ("erased_at", "kyc_expires_at", "kyc_verified_at", "kyc_status", "country", "full_name"):
        op.drop_column("users", column, schema="gateway")
