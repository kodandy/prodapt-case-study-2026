"""Tests for explain_payout, from docs/specs/sq4.md section 5 (hand-built data, recording fake LLMs)."""

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.scope import Scope
from ops_ai.settlement.service import SettlementExplanation, explain_payout
from ops_ai.settlement.statement import SettlementLine, SettlementStatement
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SCOPE = Scope(merchant_id=MERCHANT)
BASE = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
QUESTION = "Why is my October payout lower than expected?"
NARRATION = "narration from the recording llm: your payout is 1.00 USD"
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
    "(:payment_id, :merchant_id, :amount_minor, :currency, 'card', 'succeeded', 1, :occurred_at)"
)


@dataclass(frozen=True)
class Spec:
    amount_minor: int
    currency: str = "USD"
    refunded: bool = False
    chargeback: bool = False
    fact: bool = True
    fact_amount_minor: int | None = None


# USD cents after conversion: 10000, 10870, 6700, 5000, 2174, 1190
MIXED = (
    Spec(10_000),
    Spec(10_000, "EUR"),
    Spec(10_000, "JPY", refunded=True),
    Spec(5_000, chargeback=True),
    Spec(2_000, "EUR", refunded=True, chargeback=True),
    Spec(100_000, "INR", refunded=True),
)
MIXED_SUMMARY = (
    "Your 2026-10 payout is 144.32 USD, 204.60 USD less than the 348.92 USD you would expect from gross "
    "sales of 359.34 USD minus the 2.90% MDR. "
    "Fixed fees: 1.80 USD (6 payments at 0.30 USD each). "
    "FX conversion: 4.19 USD on 4 non-USD payments (rate set fx-2026-10). "
    "Refunds: 78.90 USD on 2 payments. "
    "Chargebacks: 71.74 USD on 2 payments. "
    "Chargeback fees: 30.00 USD (2 at 15.00 USD each). "
    "Rolling reserve: 17.97 USD held (5.00% of gross), released in a later payout."
)
MIXED_REFS = [
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
# one USD payment of 1234.50: mdr 3580.05 -> 3580, reserve 6172.5 -> 6173
SIMPLE = (Spec(123_450),)
SIMPLE_SUMMARY = (
    "Your 2026-10 payout is 1136.67 USD, 62.03 USD less than the 1198.70 USD you would expect from gross "
    "sales of 1234.50 USD minus the 2.90% MDR. "
    "Fixed fees: 0.30 USD (1 payments at 0.30 USD each). "
    "Rolling reserve: 61.73 USD held (5.00% of gross), released in a later payout."
)


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


def _load(session_factory: sessionmaker[Session], specs: tuple[Spec, ...], period: str = "2026-10") -> None:
    with session_factory.begin() as session:
        for offset, spec in enumerate(specs):
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
            fact_amount = spec.amount_minor if spec.fact_amount_minor is None else spec.fact_amount_minor
            if spec.fact:
                session.execute(
                    INSERT_FACT,
                    {
                        "payment_id": payment_id,
                        "merchant_id": MERCHANT,
                        "amount_minor": fact_amount,
                        "currency": spec.currency,
                        "occurred_at": captured_at,
                    },
                )


def _explain(
    session_factory: sessionmaker[Session],
    llm: RecordingLlm | DownLlm,
    period: str | None = None,
    question: str = QUESTION,
) -> SettlementExplanation | None:
    with session_factory() as session:
        return explain_payout(session, SCOPE, question, period, llm)


def _found(
    session_factory: sessionmaker[Session],
    llm: RecordingLlm | DownLlm,
    period: str | None = None,
    question: str = QUESTION,
) -> SettlementExplanation:
    result = _explain(session_factory, llm, period, question)
    assert isinstance(result, SettlementExplanation)
    return result


def _summaries(result: SettlementExplanation) -> dict[str, str]:
    return {item.ref: item.summary for item in result.evidence}


# --- no statement ----------------------------------------------------------------------------------------


def test_explain_payout_returns_none_without_a_statement_and_never_calls_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    llm = RecordingLlm()

    # Act
    result = _explain(session_factory, llm)

    # Assert
    assert result is None
    assert llm.calls == []


def test_explain_payout_returns_none_for_a_period_without_entries(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, SIMPLE)
    llm = RecordingLlm()

    # Act
    result = _explain(session_factory, llm, "2026-09")

    # Assert
    assert result is None
    assert llm.calls == []


# --- the tool call ---------------------------------------------------------------------------------------


def test_explain_payout_without_a_period_records_the_tool_call_with_latest(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, SIMPLE)

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert [(call.name, call.args) for call in result.tool_calls] == [
        ("explain_settlement", {"period": "latest"})
    ]


def test_explain_payout_with_a_period_records_the_tool_call_with_that_period(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, SIMPLE, period="2026-09")

    # Act
    result = _found(session_factory, RecordingLlm(), "2026-09")

    # Assert
    assert [(call.name, call.args) for call in result.tool_calls] == [
        ("explain_settlement", {"period": "2026-09"})
    ]
    assert result.statement.period == "2026-09"


# --- the verified summary --------------------------------------------------------------------------------


def test_explain_payout_summary_lists_every_non_zero_line_in_line_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert result.summary == MIXED_SUMMARY


def test_explain_payout_summary_skips_zero_lines_and_rounds_the_reserve_half_up(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, SIMPLE)

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert result.summary == SIMPLE_SUMMARY
    omitted = ("Refunds:", "Chargebacks:", "FX conversion:", "Chargeback fees:")
    assert [label for label in omitted if label in result.summary] == []


def test_explain_payout_summary_is_the_same_with_and_without_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    with_llm = _found(session_factory, RecordingLlm())
    without_llm = _found(session_factory, DownLlm())

    # Assert
    assert with_llm.summary == without_llm.summary == MIXED_SUMMARY
    assert NARRATION not in with_llm.summary


def test_explain_payout_summary_with_a_zero_gap_says_the_payout_is_the_same_as_expected(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: with the standard contract a zero gap needs no captured payment, so the tool is stubbed
    lines = (
        SettlementLine("gross", 100_000, 4),
        SettlementLine("mdr", -2_900, 4),
        SettlementLine("fixed_fees", 0, 0),
        SettlementLine("fx", 0, 0),
        SettlementLine("refunds", 0, 0),
        SettlementLine("chargebacks", 0, 0),
        SettlementLine("chargeback_fees", 0, 0),
        SettlementLine("reserve", 0, 0),
    )
    statement = SettlementStatement(
        period="2026-10",
        currency="USD",
        contract_version="std-2026-10",
        fx_rate_id="fx-2026-10",
        lines=lines,
        gross_minor=100_000,
        expected_minor=97_100,
        net_minor=97_100,
        gap_minor=0,
        verified=True,
        mismatches=(),
        projection_checked=4,
    )

    def stub(_session: Session, _scope: Scope, _period: str | None) -> SettlementStatement | None:
        return statement

    monkeypatch.setattr("ops_ai.settlement.tools.explain_settlement", stub)
    monkeypatch.setattr("ops_ai.settlement.service.explain_settlement", stub, raising=False)

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert result.summary == (
        "Your 2026-10 payout is 971.00 USD, the same as expected from gross sales of 1000.00 USD "
        "minus the 2.90% MDR."
    )
    assert [item.ref for item in result.evidence] == [
        "statement:2026-10",
        "contract:std-2026-10",
        "crosscheck:2026-10",
    ]


# --- the verified evidence -------------------------------------------------------------------------------


def test_explain_payout_evidence_for_the_mixed_statement_has_the_specified_refs_in_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert [item.ref for item in result.evidence] == MIXED_REFS
    assert all(isinstance(item, EvidenceItem) for item in result.evidence)


def test_explain_payout_evidence_leaves_out_zero_lines_and_the_fx_rate_set_when_there_is_no_fx(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, SIMPLE)

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert [item.ref for item in result.evidence] == [
        "statement:2026-10",
        "line:fixed_fees",
        "line:reserve",
        "contract:std-2026-10",
        "crosscheck:2026-10",
    ]


def test_explain_payout_statement_evidence_carries_gross_expected_net_and_gap(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    summary = _summaries(_found(session_factory, RecordingLlm()))["statement:2026-10"]

    # Assert
    assert [money in summary for money in ("359.34 USD", "348.92 USD", "144.32 USD", "204.60 USD")] == [
        True
    ] * 4


@pytest.mark.parametrize(
    ("kind", "amount", "count"),
    [
        ("fixed_fees", "1.80 USD", 6),
        ("fx", "4.19 USD", 4),
        ("refunds", "78.90 USD", 2),
        ("chargebacks", "71.74 USD", 2),
        ("chargeback_fees", "30.00 USD", 2),
        ("reserve", "17.97 USD", 6),
    ],
)
def test_explain_payout_line_evidence_carries_the_absolute_amount_and_the_count(
    session_factory: sessionmaker[Session], kind: str, amount: str, count: int
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    summary = _summaries(_found(session_factory, RecordingLlm()))[f"line:{kind}"]

    # Assert
    assert amount in summary
    assert f"-{amount}" not in summary
    assert re.search(rf"\b{count}\b", summary) is not None


def test_explain_payout_contract_evidence_carries_the_terms(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    summary = _summaries(_found(session_factory, RecordingLlm()))["contract:std-2026-10"]

    # Assert
    assert [term in summary for term in ("2.90%", "0.30 USD", "15.00 USD", "5.00%")] == [True] * 4


def test_explain_payout_crosscheck_evidence_carries_the_number_of_payments_matched(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    summary = _summaries(_found(session_factory, RecordingLlm()))["crosscheck:2026-10"]

    # Assert
    assert re.search(r"\b6\b", summary) is not None


# --- the LLM ---------------------------------------------------------------------------------------------


def test_explain_payout_with_a_verified_statement_asks_the_llm_once_with_the_question_and_the_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)
    llm = RecordingLlm()

    # Act
    result = _found(session_factory, llm)

    # Assert
    assert [(call.question, call.evidence) for call in llm.calls] == [(QUESTION, result.evidence)]
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)


def test_explain_payout_with_an_unavailable_llm_falls_back_with_the_same_fields(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, MIXED)
    with_llm = _found(session_factory, RecordingLlm())
    down = DownLlm()

    # Act
    fallback = _found(session_factory, down)

    # Assert
    assert (fallback.mode, fallback.text, fallback.model) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert fallback.statement == with_llm.statement
    assert fallback.summary == with_llm.summary
    assert fallback.evidence == with_llm.evidence
    assert fallback.tool_calls == with_llm.tool_calls


def test_explain_payout_carries_the_verified_statement(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _load(session_factory, MIXED)

    # Act
    statement = _found(session_factory, RecordingLlm()).statement

    # Assert
    assert (statement.verified, statement.mismatches, statement.net_minor, statement.gap_minor) == (
        True,
        (),
        14_432,
        20_460,
    )


# --- the unverified statement ----------------------------------------------------------------------------


def test_explain_payout_with_an_unverified_statement_says_so_and_never_calls_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, (*MIXED[:5], Spec(100_000, "INR", refunded=True, fact_amount_minor=99_999)))
    llm = RecordingLlm()

    # Act
    result = _found(session_factory, llm)

    # Assert
    assert result.summary == (
        "The 2026-10 settlement could not be verified (projection_mismatch); "
        "contact support with reference 2026-10."
    )
    assert llm.calls == []
    assert (result.mode, result.text, result.model) == ("t2_fallback", None, None)
    assert (result.statement.verified, result.statement.mismatches) == (False, ("projection_mismatch",))


def test_explain_payout_joins_several_mismatch_codes_with_a_comma_and_a_space(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, (Spec(10_000), Spec(5_000, "GBP", fact=False)))

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert result.summary == (
        "The 2026-10 settlement could not be verified (unsupported_currency, projection_mismatch); "
        "contact support with reference 2026-10."
    )


def test_explain_payout_unverified_evidence_is_the_statement_and_a_crosscheck_naming_the_codes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, (Spec(10_000), Spec(5_000, "GBP", fact=False)))

    # Act
    result = _found(session_factory, RecordingLlm())

    # Assert
    assert [item.ref for item in result.evidence] == ["statement:2026-10", "crosscheck:2026-10"]
    crosscheck = _summaries(result)["crosscheck:2026-10"]
    assert "unsupported_currency" in crosscheck
    assert "projection_mismatch" in crosscheck


def test_explain_payout_unverified_statement_with_an_unavailable_llm_makes_no_call_either(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _load(session_factory, (Spec(10_000, fact=False),))
    down = DownLlm()

    # Act
    result = _found(session_factory, down)

    # Assert
    assert down.calls == []
    assert (result.mode, result.text, result.model) == ("t2_fallback", None, None)
