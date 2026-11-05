"""wallets, fx rates and payouts

Revision ID: 5e2f8a1c9d47
Revises: c41e7a9d2b85
"""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "5e2f8a1c9d47"
down_revision: str | None = "c41e7a9d2b85"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literal copy of gateway.payments.fx.service.demo_rates(): a migration must not change when app code does.
DEMO_RATES_VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)
DEMO_RATES = (
    ("demo-EURINR-v1", "EUR", "INR", "90.4891304348"),
    ("demo-EURJPY-v1", "EUR", "JPY", "164.6739130435"),
    ("demo-EURUSD-v1", "EUR", "USD", "1.0869565217"),
    ("demo-INREUR-v1", "INR", "EUR", "0.0110510511"),
    ("demo-INRJPY-v1", "INR", "JPY", "1.8198198198"),
    ("demo-INRUSD-v1", "INR", "USD", "0.0120120120"),
    ("demo-JPYEUR-v1", "JPY", "EUR", "0.0060726073"),
    ("demo-JPYINR-v1", "JPY", "INR", "0.5495049505"),
    ("demo-JPYUSD-v1", "JPY", "USD", "0.0066006601"),
    ("demo-USDEUR-v1", "USD", "EUR", "0.9200000000"),
    ("demo-USDINR-v1", "USD", "INR", "83.2500000000"),
    ("demo-USDJPY-v1", "USD", "JPY", "151.5000000000"),
)


def upgrade() -> None:
    fx_rates = op.create_table(
        "fx_rates",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=20, scale=10), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("rate > 0", name=op.f("ck_fx_rates_rate_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fx_rates")),
        sa.UniqueConstraint(
            "base_currency",
            "quote_currency",
            "valid_from",
            name=op.f("uq_fx_rates_base_currency_quote_currency_valid_from"),
        ),
        schema="gateway",
    )
    op.bulk_insert(
        fx_rates,
        [
            {
                "id": rate_id,
                "base_currency": base,
                "quote_currency": quote,
                "rate": Decimal(rate),
                "valid_from": DEMO_RATES_VALID_FROM,
            }
            for rate_id, base, quote, rate in DEMO_RATES
        ],
    )
    op.create_table(
        "payouts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("destination_masked", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_payouts_amount_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"], ["gateway.ledger_accounts.id"], name=op.f("fk_payouts_account_id_ledger_accounts")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["gateway.users.id"], name=op.f("fk_payouts_user_id_users")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_payouts")),
        sa.UniqueConstraint("user_id", "idempotency_key", name=op.f("uq_payouts_user_id_idempotency_key")),
        schema="gateway",
    )
    op.create_table(
        "wallet_top_ups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_minor > 0", name=op.f("ck_wallet_top_ups_amount_positive")),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["gateway.ledger_accounts.id"],
            name=op.f("fk_wallet_top_ups_account_id_ledger_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["gateway.users.id"], name=op.f("fk_wallet_top_ups_user_id_users")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallet_top_ups")),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name=op.f("uq_wallet_top_ups_user_id_idempotency_key")
        ),
        schema="gateway",
    )
    op.add_column(
        "journal_entries", sa.Column("fx_rate_id", sa.String(length=64), nullable=True), schema="gateway"
    )
    op.create_foreign_key(
        op.f("fk_journal_entries_fx_rate_id_fx_rates"),
        "journal_entries",
        "fx_rates",
        ["fx_rate_id"],
        ["id"],
        source_schema="gateway",
        referent_schema="gateway",
    )
    op.add_column(
        "transfers", sa.Column("recipient_currency", sa.String(length=3), nullable=True), schema="gateway"
    )
    op.add_column(
        "transfers", sa.Column("recipient_amount_minor", sa.BigInteger(), nullable=True), schema="gateway"
    )
    op.add_column("transfers", sa.Column("fx_rate_id", sa.String(length=64), nullable=True), schema="gateway")
    op.create_foreign_key(
        op.f("fk_transfers_fx_rate_id_fx_rates"),
        "transfers",
        "fx_rates",
        ["fx_rate_id"],
        ["id"],
        source_schema="gateway",
        referent_schema="gateway",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_transfers_fx_rate_id_fx_rates"), "transfers", schema="gateway", type_="foreignkey"
    )
    op.drop_column("transfers", "fx_rate_id", schema="gateway")
    op.drop_column("transfers", "recipient_amount_minor", schema="gateway")
    op.drop_column("transfers", "recipient_currency", schema="gateway")
    op.drop_constraint(
        op.f("fk_journal_entries_fx_rate_id_fx_rates"),
        "journal_entries",
        schema="gateway",
        type_="foreignkey",
    )
    op.drop_column("journal_entries", "fx_rate_id", schema="gateway")
    op.drop_table("wallet_top_ups", schema="gateway")
    op.drop_table("payouts", schema="gateway")
    op.drop_table("fx_rates", schema="gateway")
