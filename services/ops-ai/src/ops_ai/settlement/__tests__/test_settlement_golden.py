"""Golden tests for the planted SQ4 merchant (docs/specs/sq4.md section 6, Golden).

The expectations are recomputed here from generate(seed=42) with exact fractions, not by the code under test.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, MockLlm
from ops_ai.db import make_session_factory
from ops_ai.scope import Scope
from ops_ai.seed import merchant_uuid, seed_dataset
from ops_ai.settlement.seed import seed_settlement
from ops_ai.settlement.service import SettlementExplanation, explain_payout
from ops_ai.settlement.statement import SettlementStatement
from ops_ai.settlement.tools import explain_settlement
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import DATA_SCHEMAS
from scripts.generate_dataset import CanonicalTransaction, ScenarioLog, generate

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
QUESTION = "Why is my October payout lower than expected?"
SQ4_MERCHANT = "mch_0003"
USD_PER_UNIT = {
    "USD": Fraction(1),
    "EUR": Fraction(10870, 10000),
    "INR": Fraction(119, 10000),
    "JPY": Fraction(67, 10000),
}
MINOR_EXPONENT = {"USD": 2, "EUR": 2, "INR": 2, "JPY": 0}
KINDS = ("gross", "mdr", "fixed_fees", "fx", "refunds", "chargebacks", "chargeback_fees", "reserve")


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text="narration", model="recording-model-1")


@dataclass(frozen=True)
class Golden:
    session_factory: sessionmaker[Session]
    log: ScenarioLog
    inserted: int
    reseeded: int


@dataclass(frozen=True)
class Expected:
    """What the log says one merchant's October statement must be: kind -> (amount, count)."""

    lines: dict[str, tuple[int, int]]
    entries: int
    net: int
    expected: int
    gap: int


def _half_up(value: Fraction) -> int:
    return math.floor(value + Fraction(1, 2))


def _usd_cents(tx: CanonicalTransaction) -> int:
    in_units = Fraction(tx.amount_minor, 10 ** MINOR_EXPONENT[tx.currency])
    return _half_up(in_units * USD_PER_UNIT[tx.currency] * 100)


def _captured(log: ScenarioLog, merchant: str) -> list[CanonicalTransaction]:
    shift = ANCHOR - log.planted.sq1_window_end
    return [
        tx
        for tx in log.transactions
        if tx.merchant_id == merchant
        and tx.record_kind == "merchant_payment"
        and tx.payment_method == "card"
        and tx.status in ("success", "reversed")
        and tx.timestamp + shift <= ANCHOR
    ]


def _expected(log: ScenarioLog, merchant: str) -> Expected:
    captured = _captured(log, merchant)
    gross = sum(_usd_cents(tx) for tx in captured)
    foreign = [tx for tx in captured if tx.currency != "USD"]
    charged_back = [tx for tx in captured if tx.chargeback_flag]
    refunded = [tx for tx in captured if tx.status == "reversed" and not tx.chargeback_flag]
    lines = {
        "gross": (gross, len(captured)),
        "mdr": (-_half_up(Fraction(gross * 290, 10_000)), len(captured)),
        "fixed_fees": (-30 * len(captured), len(captured)),
        "fx": (-_half_up(Fraction(sum(_usd_cents(tx) for tx in foreign) * 200, 10_000)), len(foreign)),
        "refunds": (-sum(_usd_cents(tx) for tx in refunded), len(refunded)),
        "chargebacks": (-sum(_usd_cents(tx) for tx in charged_back), len(charged_back)),
        "chargeback_fees": (-1_500 * len(charged_back), len(charged_back)),
        "reserve": (-_half_up(Fraction(gross * 500, 10_000)), len(captured)),
    }
    net = sum(amount for amount, _count in lines.values())
    expected = lines["gross"][0] + lines["mdr"][0]
    return Expected(lines=lines, entries=len(captured), net=net, expected=expected, gap=expected - net)


def _observed_lines(statement: SettlementStatement) -> dict[str, tuple[int, int]]:
    return {line.kind: (line.amount_minor, line.count) for line in statement.lines}


@pytest.fixture(scope="module")
def golden(engine: Engine) -> Golden:
    """Seeds generate(seed=42) once per module; function-scoped fixtures of other modules truncate it."""
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) AND tablename <> 'alembic_version'"
            ),
            {"schemas": list(DATA_SCHEMAS)},
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    session_factory = make_session_factory(engine)
    log = generate(seed=42)
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR)
    inserted = seed_settlement(session_factory, log, ANCHOR)
    reseeded = seed_settlement(session_factory, log, ANCHOR)
    return Golden(session_factory=session_factory, log=log, inserted=inserted, reseeded=reseeded)


def _statement(
    golden: Golden, merchant: str = SQ4_MERCHANT, period: str | None = None
) -> SettlementStatement:
    with golden.session_factory() as session:
        result = explain_settlement(session, Scope(merchant_id=merchant_uuid(merchant)), period)
    assert isinstance(result, SettlementStatement)
    return result


def _explanation(
    golden: Golden, llm: MockLlm | RecordingLlm, merchant: str = SQ4_MERCHANT
) -> SettlementExplanation:
    with golden.session_factory() as session:
        result = explain_payout(session, Scope(merchant_id=merchant_uuid(merchant)), QUESTION, None, llm)
    assert isinstance(result, SettlementExplanation)
    return result


# --- the seed --------------------------------------------------------------------------------------------


def test_golden_the_seeded_entry_count_is_the_captured_card_records_at_or_before_the_anchor(
    golden: Golden,
) -> None:
    # Arrange
    shift = ANCHOR - golden.log.planted.sq1_window_end
    expected = sum(
        1
        for tx in golden.log.transactions
        if tx.record_kind == "merchant_payment"
        and tx.payment_method == "card"
        and tx.merchant_id is not None
        and tx.status in ("success", "reversed")
        and tx.timestamp + shift <= ANCHOR
    )

    # Act
    inserted = golden.inserted

    # Assert
    assert expected > 1000
    assert inserted == expected


def test_golden_seeding_the_settlement_again_inserts_nothing(golden: Golden) -> None:
    # Arrange / Act
    reseeded = golden.reseeded

    # Assert
    assert reseeded == 0


# --- the planted merchant --------------------------------------------------------------------------------


def test_golden_the_planted_merchant_without_a_period_gets_october_verified(golden: Golden) -> None:
    # Arrange / Act
    statement = _statement(golden)

    # Assert
    assert statement.period == "2026-10"
    assert (statement.verified, statement.mismatches) == (True, ())
    assert (statement.currency, statement.contract_version, statement.fx_rate_id) == (
        "USD",
        "std-2026-10",
        "fx-2026-10",
    )


def test_golden_the_planted_merchants_lines_equal_the_ones_recomputed_from_the_log(golden: Golden) -> None:
    # Arrange
    expected = _expected(golden.log, SQ4_MERCHANT)

    # Act
    statement = _statement(golden)

    # Assert
    assert tuple(line.kind for line in statement.lines) == KINDS
    assert _observed_lines(statement) == expected.lines
    assert (statement.gross_minor, statement.expected_minor, statement.net_minor, statement.gap_minor) == (
        expected.lines["gross"][0],
        expected.expected,
        expected.net,
        expected.gap,
    )


def test_golden_the_projection_check_covers_every_entry_of_the_planted_merchant(golden: Golden) -> None:
    # Arrange
    expected = _expected(golden.log, SQ4_MERCHANT)

    # Act
    statement = _statement(golden)

    # Assert
    assert expected.entries > 60
    assert statement.projection_checked == expected.entries
    assert statement.lines[0].count == expected.entries


def test_golden_the_planted_merchants_returns_and_fx_are_at_least_the_planted_amounts(golden: Golden) -> None:
    # Arrange
    captured = _captured(golden.log, SQ4_MERCHANT)
    chargebacks = sum(1 for tx in captured if tx.chargeback_flag)
    refunds = sum(1 for tx in captured if tx.status == "reversed" and not tx.chargeback_flag)
    foreign = sum(1 for tx in captured if tx.currency != "USD")

    # Act
    counts = {line.kind: line.count for line in _statement(golden).lines}

    # Assert
    assert (counts["chargebacks"], counts["refunds"], counts["fx"]) == (chargebacks, refunds, foreign)
    assert chargebacks >= 3
    assert refunds >= 5
    assert foreign >= 10


def test_golden_the_refund_chargeback_and_fx_amounts_are_negative(golden: Golden) -> None:
    # Arrange / Act
    amounts = {line.kind: line.amount_minor for line in _statement(golden).lines}

    # Assert
    assert [amounts[kind] < 0 for kind in ("refunds", "chargebacks", "fx")] == [True, True, True]


def test_golden_the_lines_sum_to_the_net_and_the_gap_to_the_expected_payout_is_positive(
    golden: Golden,
) -> None:
    # Arrange / Act
    statement = _statement(golden)

    # Assert
    assert sum(line.amount_minor for line in statement.lines) == statement.net_minor
    assert statement.gap_minor == statement.expected_minor - statement.net_minor
    assert statement.gap_minor > 0
    assert statement.net_minor < statement.expected_minor


# --- the explanation -------------------------------------------------------------------------------------


def test_golden_the_summary_names_the_refunds_chargebacks_and_fx_conversion(golden: Golden) -> None:
    # Arrange / Act
    result = _explanation(golden, MockLlm())

    # Assert
    assert result.summary.startswith("Your 2026-10 payout is ")
    omitted = [
        label for label in ("Refunds:", "Chargebacks:", "FX conversion:") if label not in result.summary
    ]
    assert omitted == []


def test_golden_the_summary_money_is_the_recomputed_net_gap_and_gross(golden: Golden) -> None:
    # Arrange
    expected = _expected(golden.log, SQ4_MERCHANT)

    # Act
    summary = _explanation(golden, MockLlm()).summary

    # Assert
    assert summary.startswith(
        f"Your 2026-10 payout is {expected.net / 100:.2f} USD, {expected.gap / 100:.2f} USD less than the "
        f"{expected.expected / 100:.2f} USD you would expect from gross sales of "
        f"{expected.lines['gross'][0] / 100:.2f} USD minus the 2.90% MDR."
    )


def test_golden_the_evidence_cites_the_statement_the_returns_the_fx_line_and_the_crosscheck(
    golden: Golden,
) -> None:
    # Arrange / Act
    result = _explanation(golden, MockLlm())

    # Assert
    refs = [item.ref for item in result.evidence]
    assert refs == [
        "statement:2026-10",
        "line:fixed_fees",
        "line:fx",
        "line:refunds",
        "line:chargebacks",
        "line:chargeback_fees",
        "line:reserve",
        "contract:std-2026-10",
        "fx:fx-2026-10",
        "crosscheck:2026-10",
    ]
    assert [(call.name, call.args) for call in result.tool_calls] == [
        ("explain_settlement", {"period": "latest"})
    ]


def test_golden_the_mock_llm_narrates_a_verified_statement(golden: Golden) -> None:
    # Arrange / Act
    result = _explanation(golden, MockLlm())

    # Assert
    assert (result.mode, result.model) == ("llm", "mock")
    assert result.text is not None
    assert "statement:2026-10" in result.text


# --- tenancy ---------------------------------------------------------------------------------------------


def test_golden_every_seeded_merchant_sees_its_own_verified_statement_and_nobody_elses(
    golden: Golden,
) -> None:
    # Arrange
    merchants = sorted({tx.merchant_id for tx in golden.log.transactions if tx.merchant_id is not None})
    expected = {m: _expected(golden.log, m) for m in merchants}
    seeded = [m for m in merchants if expected[m].entries > 0]

    # Act
    statements = {m: _statement(golden, m) for m in seeded}

    # Assert
    assert len(seeded) >= 30
    assert {m: _observed_lines(s) for m, s in statements.items()} == {m: expected[m].lines for m in seeded}
    assert [m for m, s in statements.items() if not s.verified] == []
    assert len({s.gross_minor for s in statements.values()}) > 1


# --- a corrupted projection ------------------------------------------------------------------------------


def test_golden_a_changed_projection_amount_makes_the_statement_unverified_and_skips_the_llm(
    golden: Golden,
) -> None:
    # Arrange
    victim = next(tx for tx in _captured(golden.log, SQ4_MERCHANT) if tx.status == "reversed")
    llm = RecordingLlm()
    params = {"payment_id": victim.transaction_id}
    with golden.session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE ops_ai.payment_facts SET amount_minor = amount_minor + 1 "
                "WHERE payment_id = :payment_id"
            ),
            params,
        )

    # Act
    try:
        result = _explanation(golden, llm)
    finally:
        with golden.session_factory.begin() as session:
            session.execute(
                text(
                    "UPDATE ops_ai.payment_facts SET amount_minor = amount_minor - 1 "
                    "WHERE payment_id = :payment_id"
                ),
                params,
            )

    # Assert
    assert result.statement.verified is False
    assert result.statement.mismatches == ("projection_mismatch",)
    assert llm.calls == []
    assert (result.mode, result.text, result.model) == ("t2_fallback", None, None)
    assert result.summary == (
        "The 2026-10 settlement could not be verified (projection_mismatch); "
        "contact support with reference 2026-10."
    )
