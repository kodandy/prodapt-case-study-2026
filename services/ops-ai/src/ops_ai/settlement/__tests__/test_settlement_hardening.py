"""Tests for docs/specs/sq4.md "Hardening after the security review": H1 (negative net wording), the H2
method cross-check, recompute ordering and crosscheck evidence, and the H3 latest-period filter."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.scope import Scope
from ops_ai.settlement.contract import FX_SNAPSHOT, STANDARD_CONTRACT, Contract, FxSnapshot
from ops_ai.settlement.service import SettlementExplanation, explain_payout
from ops_ai.settlement.statement import EntryRow, SettlementLine, SettlementStatement
from ops_ai.settlement.tools import MAX_SETTLEMENT_ENTRIES, explain_settlement
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SCOPE = Scope(merchant_id=MERCHANT)
BASE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
QUESTION = "Why is my October payout lower than expected?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"

INSERT_ENTRY = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, 'seed')"
)
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :method, :state, 1, :occurred_at)"
)
DROP_PERIOD_CONSTRAINT = text(
    "ALTER TABLE ops_ai.settlement_entries DROP CONSTRAINT IF EXISTS ck_settlement_entries_period_format"
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
    fact: bool = True


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
# Two USD 5.00 payments, one charged back: gross 1000, mdr -29, fixed fees -60, chargebacks -500,
# chargeback fees -1500, reserve -50 -> net -1139, expected 971, gap 2110.
NEGATIVE = (Spec(500), Spec(500, chargeback=True))
NEGATIVE_SUMMARY = (
    "Your 2026-10 settlement has no payout: you owe 11.39 USD, 21.10 USD less than the 9.71 USD you would "
    "expect from gross sales of 10.00 USD minus the 2.90% MDR. "
    "Fixed fees: 0.60 USD (2 payments at 0.30 USD each). "
    "Chargebacks: 5.00 USD on 1 payments. "
    "Chargeback fees: 15.00 USD (1 at 15.00 USD each). "
    "Rolling reserve: 0.50 USD held (5.00% of gross), released in a later payout."
)
NEGATIVE_STATEMENT_EVIDENCE = (
    "Settlement statement 2026-10 in USD: gross 10.00 USD, expected after MDR 9.71 USD, "
    "net balance owed by you 11.39 USD, gap 21.10 USD"
)
# One USD payment of 0.33: mdr 0.957 -> 1, fixed fee 30, reserve 1.65 -> 2: net 33 - 1 - 30 - 2 = 0.
ZERO_NET = (Spec(33),)
ZERO_NET_SUMMARY = (
    "Your 2026-10 payout is 0.00 USD, 0.32 USD less than the 0.32 USD you would expect from gross sales "
    "of 0.33 USD minus the 2.90% MDR. "
    "Fixed fees: 0.30 USD (1 payments at 0.30 USD each). "
    "Rolling reserve: 0.02 USD held (5.00% of gross), released in a later payout."
)
MIXED_STATEMENT_EVIDENCE = (
    "Settlement statement 2026-10 in USD: gross 359.34 USD, expected after MDR 348.92 USD, "
    "net payout 144.32 USD, gap 204.60 USD"
)
MIXED_CROSSCHECK_EVIDENCE = (
    "6 payments of 2026-10 matched the payment projection (same amount, currency and card method, "
    "succeeded); refund and chargeback flags come from the settlement report only"
)


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


def _add(
    session_factory: sessionmaker[Session],
    spec: Spec,
    *,
    period: str = "2026-10",
    captured_at: datetime = BASE,
    method: str = "card",
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_ENTRY,
            {
                "payment_id": payment_id,
                "merchant_id": MERCHANT,
                "amount_minor": spec.amount_minor,
                "currency": spec.currency,
                "captured_at": captured_at,
                "statement_period": period,
                "refunded": spec.refunded,
                "chargeback": spec.chargeback,
            },
        )
        if spec.fact:
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": MERCHANT,
                    "amount_minor": spec.amount_minor,
                    "currency": spec.currency,
                    "method": method,
                    "state": "succeeded",
                    "occurred_at": captured_at,
                },
            )
    return payment_id


def _load(session_factory: sessionmaker[Session], specs: tuple[Spec, ...]) -> None:
    for offset, spec in enumerate(specs):
        _add(session_factory, spec, captured_at=BASE + timedelta(minutes=offset))


def _bulk(session_factory: sessionmaker[Session], n: int) -> None:
    params = {"merchant_id": MERCHANT, "base": BASE, "cap": MAX_SETTLEMENT_ENTRIES, "n": n}
    with session_factory.begin() as session:
        session.execute(BULK_ENTRIES, params)
        session.execute(BULK_FACTS, params)


def _statement(session_factory: sessionmaker[Session], period: str | None = None) -> SettlementStatement:
    with session_factory() as session:
        result = explain_settlement(session, SCOPE, period)
    assert isinstance(result, SettlementStatement)
    return result


def _explanation(session_factory: sessionmaker[Session], llm: RecordingLlm) -> SettlementExplanation:
    with session_factory() as session:
        result = explain_payout(session, SCOPE, QUESTION, None, llm)
    assert isinstance(result, SettlementExplanation)
    return result


def _evidence(result: SettlementExplanation) -> dict[str, str]:
    return {item.ref: item.summary for item in result.evidence}


# --- H1: a negative net is a balance owed, never a payout --------------------------------------------------


def test_explain_settlement_keeps_a_negative_net_signed_and_verified(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)

    # Act
    statement = _statement(session_factory)

    # Assert
    assert (
        statement.gross_minor,
        statement.expected_minor,
        statement.net_minor,
        statement.gap_minor,
        statement.verified,
        statement.mismatches,
    ) == (1_000, 971, -1_139, 2_110, True, ())


def test_explain_payout_negative_net_summary_says_there_is_no_payout_and_what_is_owed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert result.summary == NEGATIVE_SUMMARY


def test_explain_payout_negative_net_summary_never_says_payout_is_or_net_payout(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert [phrase for phrase in ("payout is", "net payout") if phrase in result.summary] == []


def test_explain_payout_negative_net_statement_evidence_says_net_balance_owed_by_you(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert _evidence(result)["statement:2026-10"] == NEGATIVE_STATEMENT_EVIDENCE
    assert [phrase for item in result.evidence for phrase in ("net payout",) if phrase in item.summary] == []


def test_explain_payout_negative_net_keeps_the_evidence_refs_and_still_narrates_with_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)
    llm = RecordingLlm()

    # Act
    result = _explanation(session_factory, llm)

    # Assert
    assert [item.ref for item in result.evidence] == [
        "statement:2026-10",
        "line:fixed_fees",
        "line:chargebacks",
        "line:chargeback_fees",
        "line:reserve",
        "contract:std-2026-10",
        "crosscheck:2026-10",
    ]
    assert [(call.question, call.evidence) for call in llm.calls] == [(QUESTION, result.evidence)]
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)


def test_explain_payout_zero_net_keeps_the_payout_wording(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _load(session_factory, ZERO_NET)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert (result.statement.net_minor, result.statement.gap_minor) == (0, 32)
    assert result.summary == ZERO_NET_SUMMARY


def test_explain_payout_zero_net_statement_evidence_says_net_payout(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, ZERO_NET)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert _evidence(result)["statement:2026-10"] == (
        "Settlement statement 2026-10 in USD: gross 0.33 USD, expected after MDR 0.32 USD, "
        "net payout 0.00 USD, gap 0.32 USD"
    )


def test_explain_payout_positive_net_statement_evidence_keeps_net_payout(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert _evidence(result)["statement:2026-10"] == MIXED_STATEMENT_EVIDENCE


# --- H2: the projection row must be a card payment ---------------------------------------------------------


@pytest.mark.parametrize("method", ["wallet", "upi", "bank_transfer"])
def test_explain_settlement_flags_a_projection_row_that_is_not_a_card_payment(
    session_factory: sessionmaker[Session], method: str
) -> None:
    # Arrange: same id, amount, currency and state; only the method differs
    _add(session_factory, Spec(12_345))
    _add(session_factory, Spec(50_000), captured_at=BASE + timedelta(minutes=1), method=method)

    # Act
    statement = _statement(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches) == (False, ("projection_mismatch",))
    assert (statement.gross_minor, statement.projection_checked) == (62_345, 2)


def test_explain_settlement_reports_projection_mismatch_once_for_several_non_card_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(1_000), method="wallet")
    _add(session_factory, Spec(2_000), method="upi")
    _add(session_factory, Spec(3_000), method="bank_transfer")

    # Act
    statement = _statement(session_factory)

    # Assert
    assert statement.mismatches == ("projection_mismatch",)


def test_explain_payout_with_a_non_card_projection_row_is_unverified_and_never_calls_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(12_345), method="wallet")
    llm = RecordingLlm()

    # Act
    result = _explanation(session_factory, llm)

    # Assert
    assert result.summary == (
        "The 2026-10 settlement could not be verified (projection_mismatch); "
        "contact support with reference 2026-10."
    )
    assert llm.calls == []
    assert (result.mode, result.text, result.model) == ("t2_fallback", None, None)


def test_explain_settlement_output_for_card_payments_is_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    statement = _statement(session_factory)

    # Assert
    assert statement == MIXED_STATEMENT


# --- H2: recompute_lines inside the tool -------------------------------------------------------------------


def test_explain_settlement_runs_recompute_lines_on_the_built_lines_with_the_standard_contract_and_fx(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _load(session_factory, MIXED)
    seen: list[tuple[tuple[SettlementLine, ...], int, Contract, FxSnapshot]] = []

    def spy(
        lines: Sequence[SettlementLine], entries: Sequence[EntryRow], contract: Contract, fx: FxSnapshot
    ) -> tuple[str, ...]:
        seen.append((tuple(lines), len(entries), contract, fx))
        return ()

    monkeypatch.setattr("ops_ai.settlement.tools.recompute_lines", spy)

    # Act
    statement = _statement(session_factory)

    # Assert
    assert seen == [(MIXED_STATEMENT.lines, 6, STANDARD_CONTRACT, FX_SNAPSHOT)]
    assert statement == MIXED_STATEMENT


def test_explain_settlement_adds_line_mismatch_to_the_statement(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _load(session_factory, MIXED[:3])

    def always_mismatching(
        _lines: Sequence[SettlementLine], _entries: Sequence[EntryRow], _contract: Contract, _fx: FxSnapshot
    ) -> tuple[str, ...]:
        return ("line_mismatch",)

    monkeypatch.setattr("ops_ai.settlement.tools.recompute_lines", always_mismatching)

    # Act
    statement = _statement(session_factory)

    # Assert
    assert (statement.verified, statement.mismatches) == (False, ("line_mismatch",))


def test_explain_payout_with_a_line_mismatch_is_unverified_and_never_calls_the_llm(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _load(session_factory, MIXED[:3])

    def always_mismatching(
        _lines: Sequence[SettlementLine], _entries: Sequence[EntryRow], _contract: Contract, _fx: FxSnapshot
    ) -> tuple[str, ...]:
        return ("line_mismatch",)

    monkeypatch.setattr("ops_ai.settlement.tools.recompute_lines", always_mismatching)
    llm = RecordingLlm()

    # Act
    result = _explanation(session_factory, llm)

    # Assert
    assert result.summary == (
        "The 2026-10 settlement could not be verified (line_mismatch); "
        "contact support with reference 2026-10."
    )
    assert llm.calls == []


def test_explain_settlement_reports_the_codes_in_the_hardened_order(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
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

    def always_mismatching(
        _lines: Sequence[SettlementLine], _entries: Sequence[EntryRow], _contract: Contract, _fx: FxSnapshot
    ) -> tuple[str, ...]:
        return ("line_mismatch",)

    monkeypatch.setattr("ops_ai.settlement.tools.recompute_lines", always_mismatching)

    # Act
    statement = _statement(session_factory)

    # Assert
    assert statement.mismatches == (
        "unsupported_currency",
        "line_mismatch",
        "too_many_entries",
        "projection_mismatch",
    )


# --- H2: crosscheck evidence states its scope --------------------------------------------------------------


def test_explain_payout_verified_crosscheck_evidence_states_the_scope_of_the_check(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert _evidence(result)["crosscheck:2026-10"] == MIXED_CROSSCHECK_EVIDENCE


def test_explain_payout_verified_crosscheck_evidence_counts_the_payments_matched(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, NEGATIVE)

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert _evidence(result)["crosscheck:2026-10"] == (
        "2 payments of 2026-10 matched the payment projection (same amount, currency and card method, "
        "succeeded); refund and chargeback flags come from the settlement report only"
    )


def test_explain_payout_unverified_crosscheck_evidence_keeps_naming_the_codes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add(session_factory, Spec(12_345), method="wallet")

    # Act
    result = _explanation(session_factory, RecordingLlm())

    # Assert
    assert _evidence(result)["crosscheck:2026-10"] == (
        "1 payments of 2026-10 checked; the statement could not be verified: projection_mismatch"
    )


# --- H3: the newest period among well-formed ones ----------------------------------------------------------


def _insert_without_the_period_constraint(
    session: Session, specs_by_period: tuple[tuple[str, Spec], ...]
) -> None:
    """Insert rows of any period label; the period CHECK is dropped inside the caller's transaction, which the
    caller rolls back, so the schema is untouched."""
    session.execute(DROP_PERIOD_CONSTRAINT)
    for offset, (period, spec) in enumerate(specs_by_period):
        payment_id = uuid.uuid4()
        captured_at = BASE + timedelta(minutes=offset)
        session.execute(
            INSERT_ENTRY,
            {
                "payment_id": payment_id,
                "merchant_id": MERCHANT,
                "amount_minor": spec.amount_minor,
                "currency": spec.currency,
                "captured_at": captured_at,
                "statement_period": period,
                "refunded": spec.refunded,
                "chargeback": spec.chargeback,
            },
        )
        session.execute(
            INSERT_FACT,
            {
                "payment_id": payment_id,
                "merchant_id": MERCHANT,
                "amount_minor": spec.amount_minor,
                "currency": spec.currency,
                "method": "card",
                "state": "succeeded",
                "occurred_at": captured_at,
            },
        )


def test_explain_settlement_without_a_period_ignores_malformed_periods_that_sort_after_the_real_ones(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the CHECK constraint is dropped only inside this rolled-back transaction, because the table
    # rejects malformed periods; "2026-9", "2026-13", "<b>" and "9999-99" all sort after "2026-10"
    with session_factory() as session:
        _insert_without_the_period_constraint(
            session,
            (
                ("2026-09", Spec(1_000)),
                ("2026-10", Spec(2_000)),
                ("2026-9", Spec(900_000)),
                ("2026-13", Spec(800_000)),
                ("<b>", Spec(700_000)),
                ("9999-99", Spec(600_000)),
            ),
        )

        # Act
        statement = explain_settlement(session, SCOPE, None)
        session.rollback()

    # Assert
    assert isinstance(statement, SettlementStatement)
    assert (statement.period, statement.gross_minor, statement.verified) == ("2026-10", 2_000, True)


def test_explain_settlement_without_a_period_returns_none_when_only_malformed_periods_exist(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory() as session:
        _insert_without_the_period_constraint(
            session, (("2026-13", Spec(1_000)), ("<b>", Spec(2_000)), ("2026-9", Spec(3_000)))
        )

        # Act
        statement = explain_settlement(session, SCOPE, None)
        session.rollback()

    # Assert
    assert statement is None
