"""Tests of the map-reduce bulk analysis and its service, from docs/specs/r2-agents.md section 3 (R2-5).

analyze_bulk is pure (1000 deterministic synthetic rows, every field one token: a row is 32 tokens);
load_rows / analyze_transactions run against Postgres and a recording LLM.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.bulk.analysis import (
    DEFAULT_CHUNK_TOKENS,
    DEFAULT_TOKEN_BUDGET,
    TOP_ITEMS,
    TxRow,
    analyze_bulk,
    naive_prompt,
    render_row,
    render_summary,
    summarize_chunk,
)
from ops_ai.bulk.service import MAX_BULK_ROWS, analyze_transactions, load_rows
from ops_ai.bulk.tokens import count_tokens
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

NARRATION = "bulk narration from the recording llm"
MODEL = "recording-model-1"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
THIRD_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c03")
SERIES_START = datetime(2026, 11, 1, 0, 0, tzinfo=UTC)
ROW_TOKENS = 32
DECLINE_CODES = (
    "insufficient_funds",
    "do_not_honor",
    "expired_card",
    "stolen_card",
    "risk_blocked",
    "issuer_unavailable",
    "invalid_cvc",
)
STATES = ("succeeded", "succeeded", "succeeded", "declined", "failed", "pending")
METHODS = ("card", "wallet", "bank_transfer")
CURRENCIES = ("USD", "EUR", "GBP", "JPY")

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :method, :state, 'acq_a', :decline_code, "
    ":card_bin, NULL, 1, :occurred_at)"
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


def _synthetic_rows(count: int) -> list[TxRow]:
    rows: list[TxRow] = []
    for index in range(count):
        state = STATES[index % len(STATES)]
        failed = state in ("declined", "failed")
        rows.append(
            TxRow(
                payment_id=uuid.UUID(int=index + 1),
                amount_minor=100 + (index * 37) % 9000,
                currency=CURRENCIES[index % len(CURRENCIES)],
                method=METHODS[index % len(METHODS)],
                state=state,
                decline_code=DECLINE_CODES[(index * 3) % len(DECLINE_CODES)] if failed else None,
                card_bin=f"4{(index * 5) % 11:05d}",
                occurred_at=SERIES_START + timedelta(seconds=index * 30),
            )
        )
    return rows


# --- analyze_bulk ------------------------------------------------------------------------------------------


def test_the_defaults_are_the_specified_budgets() -> None:
    # Arrange / Act
    defaults = (DEFAULT_TOKEN_BUDGET, DEFAULT_CHUNK_TOKENS, TOP_ITEMS)

    # Assert
    assert defaults == (800, 2000, 5)


def test_a_thousand_rows_shrink_to_a_prompt_that_fits_the_default_budget() -> None:
    # Arrange
    rows = _synthetic_rows(1000)
    assert {count_tokens(render_row(row)) for row in rows} == {ROW_TOKENS}

    # Act
    analysis = analyze_bulk(rows)

    # Assert
    assert analysis.transactions == 1000
    assert analysis.within_budget is True
    assert analysis.optimized_tokens <= DEFAULT_TOKEN_BUDGET
    assert analysis.reduction > 0.9


def test_the_token_counts_are_those_of_the_naive_prompt_and_of_the_optimized_prompt() -> None:
    # Arrange
    rows = _synthetic_rows(1000)

    # Act
    analysis = analyze_bulk(rows)

    # Assert
    assert analysis.naive_tokens == count_tokens(naive_prompt(rows))
    assert analysis.naive_tokens == 2 + 1000 * ROW_TOKENS
    assert analysis.optimized_tokens == count_tokens(analysis.prompt)
    assert analysis.reduction == pytest.approx(1 - analysis.optimized_tokens / analysis.naive_tokens)


def test_the_prompt_holds_the_top_five_items_of_the_merged_summary_when_they_fit() -> None:
    # Arrange
    rows = _synthetic_rows(1000)

    # Act
    analysis = analyze_bulk(rows)

    # Assert
    assert analysis.summary == summarize_chunk(rows)
    assert analysis.prompt == render_summary(analysis.summary, TOP_ITEMS)


def test_default_chunking_of_32_token_rows_makes_17_chunks_of_at_most_62_rows() -> None:
    # Arrange: 2000 // 32 = 62 rows per chunk, 1000 rows = 16 full chunks of 62 and one of 8
    rows = _synthetic_rows(1000)

    # Act
    analysis = analyze_bulk(rows)

    # Assert
    assert analysis.chunks == 17


def test_the_chunk_size_changes_the_chunk_count_but_not_the_summary_or_the_prompt() -> None:
    # Arrange
    rows = _synthetic_rows(1000)
    whole = analyze_bulk(rows)

    # Act
    small_chunks = analyze_bulk(rows, chunk_tokens=10 * ROW_TOKENS)

    # Assert
    assert small_chunks.chunks == 100
    assert small_chunks.summary == whole.summary
    assert small_chunks.prompt == whole.prompt
    assert small_chunks.naive_tokens == whole.naive_tokens


def test_the_prompt_never_contains_a_payment_id() -> None:
    # Arrange
    rows = _synthetic_rows(1000)

    # Act
    prompt = analyze_bulk(rows).prompt

    # Assert
    assert [row.payment_id for row in rows if str(row.payment_id) in prompt] == []


def test_a_tiny_budget_falls_back_to_the_prompt_without_top_lists_and_is_not_within_budget() -> None:
    # Arrange
    rows = _synthetic_rows(1000)
    summary = summarize_chunk(rows)
    assert count_tokens(render_summary(summary, 0)) > 10

    # Act
    analysis = analyze_bulk(rows, token_budget=10)

    # Assert
    assert analysis.prompt == render_summary(summary, 0)
    assert analysis.optimized_tokens == count_tokens(render_summary(summary, 0))
    assert analysis.within_budget is False


def test_a_budget_between_two_sizes_keeps_the_largest_top_items_count_that_fits() -> None:
    # Arrange: the budget is exactly the size of the top-2 rendering; top-3 is larger
    rows = _synthetic_rows(1000)
    summary = summarize_chunk(rows)
    budget = count_tokens(render_summary(summary, 2))
    assert count_tokens(render_summary(summary, 3)) > budget

    # Act
    analysis = analyze_bulk(rows, token_budget=budget)

    # Assert
    assert analysis.prompt == render_summary(summary, 2)
    assert analysis.optimized_tokens == budget
    assert analysis.within_budget is True


def test_a_budget_just_below_the_top_five_size_drops_to_four_items() -> None:
    # Arrange
    rows = _synthetic_rows(1000)
    summary = summarize_chunk(rows)
    budget = count_tokens(render_summary(summary, TOP_ITEMS)) - 1
    assert count_tokens(render_summary(summary, 4)) <= budget

    # Act
    analysis = analyze_bulk(rows, token_budget=budget)

    # Assert
    assert analysis.prompt == render_summary(summary, 4)
    assert analysis.within_budget is True


def test_no_rows_give_no_transactions_no_chunks_and_a_zero_reduction() -> None:
    # Arrange / Act
    analysis = analyze_bulk([])

    # Assert
    assert (analysis.transactions, analysis.chunks) == (0, 0)
    assert analysis.reduction == 0.0
    assert analysis.summary == summarize_chunk([])
    assert analysis.within_budget is True


# --- load_rows ---------------------------------------------------------------------------------------------


def _insert_fact(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, number: int, occurred_at: datetime
) -> TxRow:
    row = TxRow(
        payment_id=uuid.UUID(int=number),
        amount_minor=1000 + number,
        currency="USD",
        method="card",
        state="declined" if number % 2 else "succeeded",
        decline_code="do_not_honor" if number % 2 else None,
        card_bin="400000" if number % 3 else None,
        occurred_at=occurred_at,
    )
    with session_factory.begin() as session:
        session.execute(
            INSERT_FACT,
            {
                "payment_id": row.payment_id,
                "merchant_id": merchant_id,
                "amount_minor": row.amount_minor,
                "currency": row.currency,
                "method": row.method,
                "state": row.state,
                "decline_code": row.decline_code,
                "card_bin": row.card_bin,
                "occurred_at": row.occurred_at,
            },
        )
    return row


WINDOW_START = TEST_NOW - timedelta(days=1)
WINDOW_END = TEST_NOW


def _seed_window(session_factory: sessionmaker[Session]) -> list[TxRow]:
    """The merchant's four rows inside [start, end), inserted out of order; plus rows that must not appear."""
    inside_late = _insert_fact(session_factory, MERCHANT, 4, WINDOW_START + timedelta(hours=20))
    inside_start = _insert_fact(session_factory, MERCHANT, 1, WINDOW_START)
    inside_middle = _insert_fact(session_factory, MERCHANT, 3, WINDOW_START + timedelta(hours=5))
    inside_early = _insert_fact(session_factory, MERCHANT, 2, WINDOW_START + timedelta(minutes=1))
    _insert_fact(session_factory, MERCHANT, 5, WINDOW_END)
    _insert_fact(session_factory, MERCHANT, 6, WINDOW_START - timedelta(seconds=1))
    _insert_fact(session_factory, OTHER_MERCHANT, 7, WINDOW_START + timedelta(hours=1))
    _insert_fact(session_factory, OTHER_MERCHANT, 8, WINDOW_START + timedelta(hours=2))
    return [inside_start, inside_early, inside_middle, inside_late]


def test_load_rows_returns_the_merchants_rows_in_the_half_open_window_oldest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    expected = _seed_window(session_factory)

    # Act
    with session_factory() as session:
        rows = load_rows(session, Scope(merchant_id=MERCHANT), WINDOW_START, WINDOW_END)

    # Assert
    assert rows == expected


def test_load_rows_of_a_merchant_without_payments_is_empty(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_window(session_factory)

    # Act
    with session_factory() as session:
        rows = load_rows(session, Scope(merchant_id=THIRD_MERCHANT), WINDOW_START, WINDOW_END)

    # Assert
    assert rows == []


def test_load_rows_returns_at_most_the_max_bulk_rows_keeping_the_oldest(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: MAX_BULK_ROWS + 1 rows, one second apart
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, "
                "state, entity_version, occurred_at) SELECT gen_random_uuid(), :merchant_id, 100, 'USD', "
                "'card', 'succeeded', 1, :start + (g * interval '1 second') FROM generate_series(0, :last) g"
            ),
            {"merchant_id": MERCHANT, "start": WINDOW_START, "last": MAX_BULK_ROWS},
        )

    # Act
    with session_factory() as session:
        rows = load_rows(session, Scope(merchant_id=MERCHANT), WINDOW_START, WINDOW_END)

    # Assert
    assert MAX_BULK_ROWS == 20000
    assert len(rows) == MAX_BULK_ROWS
    assert rows[0].occurred_at == WINDOW_START
    assert rows[-1].occurred_at == WINDOW_START + timedelta(seconds=MAX_BULK_ROWS - 1)


# --- analyze_transactions ----------------------------------------------------------------------------------


def test_the_llm_gets_exactly_one_evidence_item_holding_the_prompt_and_never_a_payment_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_window(session_factory)
    llm = RecordingLlm()

    # Act
    with session_factory() as session:
        result = analyze_transactions(
            session, Scope(merchant_id=MERCHANT), WINDOW_START, WINDOW_END, llm, token_budget=800
        )

    # Assert
    (request,) = llm.calls
    assert request.evidence == (EvidenceItem(ref="bulk:summary", summary=result.analysis.prompt),)
    every_id = [str(uuid.UUID(int=number)) for number in range(1, 9)]
    assert [payment_id for payment_id in every_id if payment_id in repr(request)] == []
    assert result.analysis.transactions == len(seeded)


def test_the_result_carries_the_llm_narration_and_the_analysis_of_the_loaded_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_window(session_factory)

    # Act
    with session_factory() as session:
        result = analyze_transactions(
            session, Scope(merchant_id=MERCHANT), WINDOW_START, WINDOW_END, RecordingLlm(), token_budget=300
        )

    # Assert
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)
    assert result.analysis == analyze_bulk(seeded, token_budget=300)


def test_an_unavailable_llm_gives_t2_fallback_with_no_text_and_the_same_analysis(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_window(session_factory)
    llm = DownLlm()

    # Act
    with session_factory() as session:
        result = analyze_transactions(
            session, Scope(merchant_id=MERCHANT), WINDOW_START, WINDOW_END, llm, token_budget=300
        )

    # Assert
    assert (result.mode, result.text, result.model) == ("t2_fallback", None, None)
    assert result.analysis == analyze_bulk(seeded, token_budget=300)
    assert len(llm.calls) == 1


def test_analyze_transactions_counts_only_the_scope_merchants_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_window(session_factory)

    # Act
    with session_factory() as session:
        own = analyze_transactions(
            session, Scope(merchant_id=MERCHANT), WINDOW_START, WINDOW_END, RecordingLlm(), token_budget=800
        )
        others = analyze_transactions(
            session,
            Scope(merchant_id=OTHER_MERCHANT),
            WINDOW_START,
            WINDOW_END,
            RecordingLlm(),
            token_budget=800,
        )

    # Assert
    assert (own.analysis.transactions, others.analysis.transactions) == (4, 2)
