"""Tests for the CHECK constraints on ops_ai.settlement_entries, from docs/specs/sq4.md H3."""

import uuid
from datetime import UTC, datetime

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from ops_ai.schema import TARGET_METADATA
from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import ALEMBIC_CONFIGS

SETTLEMENT_ENTRIES_REVISION = "5e8a3c1f7b42"
AMOUNT_CHECK = "ck_settlement_entries_amount_positive"
CURRENCY_CHECK = "ck_settlement_entries_currency_format"
PERIOD_CHECK = "ck_settlement_entries_period_format"
ALL_CHECKS = [AMOUNT_CHECK, CURRENCY_CHECK, PERIOD_CHECK]
CAPTURED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")

INSERT = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, :ingest_source)"
)
CHECK_NAMES = text(
    "SELECT conname FROM pg_constraint "
    "WHERE conrelid = 'ops_ai.settlement_entries'::regclass AND contype = 'c' ORDER BY conname"
)


def _params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "payment_id": uuid.uuid4(),
        "merchant_id": MERCHANT,
        "amount_minor": 123_450,
        "currency": "USD",
        "captured_at": CAPTURED_AT,
        "statement_period": "2026-10",
        "refunded": False,
        "chargeback": False,
        "ingest_source": "seed",
    }
    params.update(overrides)
    return params


def _ops_ai_config() -> Config:
    return Config(str(ALEMBIC_CONFIGS[1]))


def _check_names(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return [row.conname for row in conn.execute(CHECK_NAMES)]


# --- the constraints exist ---------------------------------------------------------------------------------


def test_settlement_entries_has_exactly_the_three_named_check_constraints(engine: Engine) -> None:
    # Arrange / Act
    names = _check_names(engine)

    # Assert
    assert names == ALL_CHECKS


def test_the_model_declares_the_three_check_constraints() -> None:
    # Arrange
    table = TARGET_METADATA.tables["ops_ai.settlement_entries"]

    # Act
    names = sorted(str(c.name) for c in table.constraints if isinstance(c, CheckConstraint))

    # Assert
    assert names == ALL_CHECKS


# --- rejected values ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("amount_minor", 0, AMOUNT_CHECK),
        ("amount_minor", -1, AMOUNT_CHECK),
        ("amount_minor", -9_007_199_254_740_993, AMOUNT_CHECK),
        ("currency", "usd", CURRENCY_CHECK),
        ("currency", "Usd", CURRENCY_CHECK),
        ("currency", "US", CURRENCY_CHECK),
        ("currency", "U$D", CURRENCY_CHECK),
        ("currency", "123", CURRENCY_CHECK),
        ("currency", "", CURRENCY_CHECK),
        ("statement_period", "2026-9", PERIOD_CHECK),
        ("statement_period", "2026-13", PERIOD_CHECK),
        ("statement_period", "2026-00", PERIOD_CHECK),
        ("statement_period", "<b>", PERIOD_CHECK),
        ("statement_period", "2026/10", PERIOD_CHECK),
        ("statement_period", "2026-1\n", PERIOD_CHECK),
        ("statement_period", "\u0662\u0660\u0662\u0666-\u0661\u0660", PERIOD_CHECK),
        ("statement_period", "", PERIOD_CHECK),
    ],
)
def test_an_invalid_value_is_rejected_by_its_named_check(
    session_factory: sessionmaker[Session], column: str, value: object, constraint: str
) -> None:
    # Arrange
    params = _params(**{column: value})

    # Act / Assert
    with pytest.raises(IntegrityError, match=constraint), session_factory.begin() as session:
        session.execute(INSERT, params)


# --- accepted values ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("amount_minor", 1),
        ("amount_minor", 9_007_199_254_740_993),
        ("currency", "EUR"),
        ("currency", "JPY"),
        ("currency", "GBP"),
        ("statement_period", "2026-01"),
        ("statement_period", "2026-09"),
        ("statement_period", "2026-12"),
        ("statement_period", "1999-10"),
    ],
)
def test_a_valid_value_is_accepted_and_read_back(
    session_factory: sessionmaker[Session], column: str, value: object
) -> None:
    # Arrange
    params = _params(**{column: value})

    # Act
    with session_factory.begin() as session:
        session.execute(INSERT, params)
    with session_factory() as session:
        row = session.execute(text("SELECT * FROM ops_ai.settlement_entries")).mappings().one()

    # Assert
    assert dict(row) == params


# --- migration ---------------------------------------------------------------------------------------------


def test_the_constraints_migration_follows_the_settlement_entries_migration() -> None:
    # Arrange
    script = ScriptDirectory.from_config(_ops_ai_config())

    # Act
    head = script.get_revision("a3f7c9e15b28")

    # Assert
    assert head.revision != SETTLEMENT_ENTRIES_REVISION
    assert head.down_revision == SETTLEMENT_ENTRIES_REVISION


def test_migration_downgrade_drops_the_three_constraints_and_upgrade_restores_them(engine: Engine) -> None:
    # Arrange
    config = _ops_ai_config()

    # Act
    try:
        command.downgrade(config, SETTLEMENT_ENTRIES_REVISION)
        after_downgrade = _check_names(engine)
    finally:
        command.upgrade(config, "head")
    after_upgrade = _check_names(engine)

    # Assert
    assert (after_downgrade, after_upgrade) == ([], ALL_CHECKS)
