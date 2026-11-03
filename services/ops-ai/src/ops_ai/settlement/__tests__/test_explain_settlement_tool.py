"""Tests for the explain_settlement tool, from docs/specs/sq4.md section 4 (hand-built data)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.scope import Scope
from ops_ai.settlement.statement import SettlementLine, SettlementStatement
from ops_ai.settlement.tools import MAX_SETTLEMENT_ENTRIES, explain_settlement
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
BASE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)

INSERT_ENTRY = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, 'seed')"
)
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, 'card', :state, 1, :occurred_at)"
)
BULK_ENTRIES = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) "
    "SELECT CAST(md5('p' || i) AS uuid), :merchant_id, CASE WHEN i > :cap THEN 7 ELSE 1000 END, 'USD', "
    "CAST(:base AS timestamptz) + i * interval '1 second', '2026-10', false, false, 'seed' "
    "FROM generate_series(1, CAST(:n AS integer)) AS i"
)
BULK_FACTS = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "entity_version, occurred_at) "
    "SELECT CAST(md5('p' || i) AS uuid), :merchant_id, CASE WHEN i > :cap THEN 7 ELSE 1000 END, 'USD', "
    "'card', 'succeeded', 1, CAST(:base AS timestamptz) FROM generate_series(1, CAST(:n AS integer)) AS i"
)


@dataclass(frozen=True)
class Spec:
    """One captured payment: the settlement entry and, unless `fact` is False, its projection row."""

    amount_minor: int
    currency: str = "USD"
    refunded: bool = False
    chargeback: bool = False


# USD cents after conversion: 10000, 10870, 6700, 5000, 2174, 1190
MIXED = (
    Spec(10_000),
    Spec(10_000, "EUR"),
    Spec(10_000, "JPY", refunded=True),
    Spec(5_000, chargeback=True),
    Spec(2_000, "EUR", refunded=True, chargeback=True),
    Spec(100_000, "INR", refunded=True),
)
MIXED_STATEMENT = SettlementStatement(
    period="2026-10",
    currency="USD",
    contract_version="std-2026-10",
    fx_rate_id="fx-2026-10",
    lines=(
        SettlementLine("gross", 35_934, 6),
        SettlementLine("mdr", -1_042, 6),
        SettlementLine("fixed_fees", -180, 6),
        SettlementLine("fx", -419, 4),
        SettlementLine("refunds", -7_890, 2),
        SettlementLine("chargebacks", -7_174, 2),
        SettlementLine("chargeback_fees", -3_000, 2),
        SettlementLine("reserve", -1_797, 6),
    ),
    gross_minor=35_934,
    expected_minor=34_892,
    net_minor=14_432,
    gap_minor=20_460,
    verified=True,
    mismatches=(),
    projection_checked=6,
)


def _add(
    session_factory: sessionmaker[Session],
    spec: Spec,
    *,
    merchant_id: uuid.UUID = MERCHANT,
    period: str = "2026-10",
    captured_at: datetime = BASE,
    fact: bool = True,
    fact_overrides: dict[str, object] | None = None,
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_ENTRY,
            {
                "payment_id": payment_id,
                "merchant_id": merchant_id,
                "amount_minor": spec.amount_minor,
                "currency": spec.currency,
                "captured_at": captured_at,
                "statement_period": period,
                "refunded": spec.refunded,
                "chargeback": spec.chargeback,
            },
        )
        if fact:
            params: dict[str, object] = {
                "payment_id": payment_id,
                "merchant_id": merchant_id,
                "amount_minor": spec.amount_minor,
                "currency": spec.currency,
                "state": "succeeded",
                "occurred_at": captured_at,
            }
            params.update(fact_overrides or {})
            session.execute(INSERT_FACT, params)
    return payment_id


def _add_all(session_factory: sessionmaker[Session], specs: tuple[Spec, ...]) -> None:
    for offset, spec in enumerate(specs):
        _add(session_factory, spec, captured_at=BASE + timedelta(minutes=offset))


def _bulk(session_factory: sessionmaker[Session], n: int) -> None:
    params = {"merchant_id": MERCHANT, "base": BASE, "cap": MAX_SETTLEMENT_ENTRIES, "n": n}
    with session_factory.begin() as session:
        session.execute(BULK_ENTRIES, params)
        session.execute(BULK_FACTS, params)


def _explain(
    session_factory: sessionmaker[Session], period: str | None = None, scope: Scope = SCOPE
) -> SettlementStatement | None:
    with session_factory() as session:
        return explain_settlement(session, scope, period)


def _found(
    session_factory: sessionmaker[Session], period: str | None = None, scope: Scope = SCOPE
) -> SettlementStatement:
    result = _explain(session_factory, period, scope)
    assert isinstance(result, SettlementStatement)
    return result


def _gross(statement: SettlementStatement) -> SettlementLine:
    return next(line for line in statement.lines if line.kind == "gross")


# --- which statement -------------------------------------------------------------------------------------


def test_max_settlement_entries_is_5000() -> None:
    # Arrange / Act
    cap = MAX_SETTLEMENT_ENTRIES

    # Assert
    assert cap == 5000


def test_explain_settlement_returns_none_when_the_merchant_has_no_entries(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    result = _explain(session_factory)

    # Assert
    assert result is None


def test_explain_settlement_returns_none_for_a_merchant_whose_only_entries_belong_to_another_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(10_000), merchant_id=OTHER_MERCHANT)

    # Act
    without_period = _explain(session_factory)
    with_period = _explain(session_factory, "2026-10")

    # Assert
    assert (without_period, with_period) == (None, None)


def test_explain_settlement_without_a_period_takes_the_newest_period_of_the_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000), period="2026-08")
    _add(session_factory, Spec(2_000), period="2026-10")
    _add(session_factory, Spec(3_000), period="2026-10")
    _add(session_factory, Spec(4_000), period="2026-09")
    _add(session_factory, Spec(99_000), period="2026-12", merchant_id=OTHER_MERCHANT)

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement.period == "2026-10"
    assert (_gross(statement).amount_minor, _gross(statement).count) == (5_000, 2)


def test_explain_settlement_orders_periods_across_a_year_boundary(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000), period="2025-12")
    _add(session_factory, Spec(2_000), period="2026-01")

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement.period == "2026-01"


def test_explain_settlement_with_a_period_takes_that_period_even_if_a_newer_one_exists(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000), period="2026-09")
    _add(session_factory, Spec(2_000), period="2026-10")

    # Act
    statement = _found(session_factory, "2026-09")

    # Assert
    assert statement.period == "2026-09"
    assert _gross(statement).amount_minor == 1_000


def test_explain_settlement_with_a_period_without_entries_returns_none(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000), period="2026-10")

    # Act
    result = _explain(session_factory, "2026-09")

    # Assert
    assert result is None


def test_explain_settlement_sums_only_the_scope_merchants_entries_of_the_period(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(10_000))
    _add(session_factory, Spec(777_700, chargeback=True), merchant_id=OTHER_MERCHANT)

    # Act
    mine = _found(session_factory)
    theirs = _found(session_factory, scope=OTHER_SCOPE)

    # Assert
    assert (mine.gross_minor, mine.projection_checked) == (10_000, 1)
    assert (theirs.gross_minor, theirs.projection_checked) == (777_700, 1)
    assert [line.count for line in mine.lines if line.kind == "chargebacks"] == [0]


# --- the statement ---------------------------------------------------------------------------------------


def test_explain_settlement_builds_and_verifies_the_mixed_statement(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_all(session_factory, MIXED)

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement == MIXED_STATEMENT


def test_explain_settlement_derives_net_expected_and_gap_from_the_lines(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_all(session_factory, MIXED)

    # Act
    statement = _found(session_factory)

    # Assert
    amounts = {line.kind: line.amount_minor for line in statement.lines}
    assert statement.net_minor == sum(amounts.values())
    assert statement.expected_minor == amounts["gross"] + amounts["mdr"]
    assert statement.gap_minor == statement.expected_minor - statement.net_minor
    assert statement.gap_minor == -sum(v for k, v in amounts.items() if k not in ("gross", "mdr"))


def test_explain_settlement_counts_a_refunded_and_charged_back_payment_as_a_chargeback_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(10_000, refunded=True, chargeback=True))

    # Act
    statement = _found(session_factory)

    # Assert
    lines = {line.kind: (line.amount_minor, line.count) for line in statement.lines}
    assert lines["refunds"] == (0, 0)
    assert lines["chargebacks"] == (-10_000, 1)


def test_explain_settlement_is_verified_and_reads_back_the_projection_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_all(session_factory, MIXED[:3])

    # Act
    statement = _found(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches, statement.projection_checked) == (True, (), 3)


# --- the projection cross-check --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fact_overrides",
    [
        {"amount_minor": 12_346},
        {"currency": "EUR"},
        {"state": "declined"},
        {"state": "unknown"},
        {"merchant_id": OTHER_MERCHANT},
    ],
    ids=["amount_differs", "currency_differs", "state_declined", "state_unknown", "other_merchant"],
)
def test_explain_settlement_flags_a_projection_row_that_disagrees(
    session_factory: sessionmaker[Session], fact_overrides: dict[str, object]
) -> None:
    # Arrange
    _add(session_factory, Spec(12_345))
    _add(
        session_factory, Spec(50_000), captured_at=BASE + timedelta(minutes=1), fact_overrides=fact_overrides
    )

    # Act
    statement = _found(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches) == (False, ("projection_mismatch",))
    assert statement.gross_minor == 62_345


def test_explain_settlement_flags_an_entry_without_a_projection_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(12_345))
    _add(session_factory, Spec(50_000), fact=False)

    # Act
    statement = _found(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches) == (False, ("projection_mismatch",))


def test_explain_settlement_reports_projection_mismatch_once_for_several_bad_entries(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000), fact=False)
    _add(session_factory, Spec(2_000), fact_overrides={"amount_minor": 1})
    _add(session_factory, Spec(3_000), fact_overrides={"state": "declined"})

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement.mismatches == ("projection_mismatch",)


def test_explain_settlement_compares_every_loaded_entry_with_the_projection(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000))
    _add(session_factory, Spec(2_000), fact=False)
    _add(session_factory, Spec(3_000))

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement.projection_checked == 3


def test_explain_settlement_ignores_the_entries_and_projection_rows_of_other_merchants_and_periods(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000))
    _add(session_factory, Spec(2_000), period="2026-09", merchant_id=OTHER_MERCHANT)

    # Act
    statement = _found(session_factory)

    # Assert
    assert (statement.verified, statement.projection_checked) == (True, 1)


def test_explain_settlement_reports_an_unsupported_currency_before_the_projection_mismatch(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(10_000))
    _add(session_factory, Spec(5_000, "GBP"), fact=False)

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement.verified is False
    assert statement.mismatches == ("unsupported_currency", "projection_mismatch")
    assert _gross(statement).amount_minor == 10_000


# --- the entry cap ---------------------------------------------------------------------------------------


def test_explain_settlement_with_exactly_the_cap_entries_is_verified(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _bulk(session_factory, MAX_SETTLEMENT_ENTRIES)

    # Act
    statement = _found(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches) == (True, ())
    assert (_gross(statement).amount_minor, _gross(statement).count) == (5_000_000, 5000)
    assert statement.projection_checked == 5000


def test_explain_settlement_with_one_entry_over_the_cap_loads_the_first_by_captured_at_and_flags_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the 5001st entry (latest captured_at, amount 7) has a projection row too
    _bulk(session_factory, MAX_SETTLEMENT_ENTRIES + 1)

    # Act
    statement = _found(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches) == (False, ("too_many_entries",))
    assert (_gross(statement).amount_minor, _gross(statement).count) == (5_000_000, 5000)
    assert statement.projection_checked == 5000


def test_explain_settlement_reports_the_codes_in_the_specified_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: entry 1 is GBP in both sources; entry 2's projection amount differs; entry 5001 is over the cap
    _bulk(session_factory, MAX_SETTLEMENT_ENTRIES + 1)
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE ops_ai.settlement_entries SET currency = 'GBP' "
                "WHERE payment_id = CAST(md5('p' || 1) AS uuid)"
            )
        )
        session.execute(
            text(
                "UPDATE ops_ai.payment_facts SET currency = 'GBP' "
                "WHERE payment_id = CAST(md5('p' || 1) AS uuid)"
            )
        )
        session.execute(
            text(
                "UPDATE ops_ai.payment_facts SET amount_minor = 1 "
                "WHERE payment_id = CAST(md5('p' || 2) AS uuid)"
            )
        )

    # Act
    statement = _found(session_factory)

    # Assert
    assert statement.verified is False
    assert statement.mismatches == ("unsupported_currency", "too_many_entries", "projection_mismatch")
