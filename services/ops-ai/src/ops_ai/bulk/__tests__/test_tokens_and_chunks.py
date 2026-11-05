"""Pure tests of token counting, chunking, map-reduce summaries and summary rendering, written from
docs/specs/r2-agents.md section 3 (R2-5).

A canonical row renders to exactly 32 tokens: 9 (uuid) + 13 (ISO timestamp) + method + state + amount +
currency (4) + `bin = <v>` (3) + `code = <v>` (3). Chunk budgets below are multiples of that.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.bulk.analysis import (
    TOP_ITEMS,
    ChunkSummary,
    TxRow,
    chunk_rows,
    merge_summaries,
    naive_prompt,
    render_row,
    render_summary,
    summarize_chunk,
)
from ops_ai.bulk.tokens import count_tokens

ROW_TOKENS = 32
BASE_TIME = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _row(number: int = 1, **overrides: object) -> TxRow:
    fields: dict[str, object] = {
        "payment_id": uuid.UUID(int=number),
        "amount_minor": 1050,
        "currency": "USD",
        "method": "card",
        "state": "declined",
        "decline_code": "insufficient_funds",
        "card_bin": "400000",
        "occurred_at": BASE_TIME + timedelta(seconds=number),
    }
    fields.update(overrides)
    return TxRow(**fields)  # type: ignore[arg-type]


def _ids(chunks: list[list[TxRow]]) -> list[list[int]]:
    return [[row.payment_id.int for row in chunk] for chunk in chunks]


# --- count_tokens ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("   \n\t ", 0),
        ("hello", 1),
        ("hello world", 2),
        ("a-b", 3),
        ("a_b", 1),
        ("bin=400000 code=-", 6),
        ("Transactions:\n", 2),
        ("00000000-0000-0000-0000-000000000001", 9),
        ("2026-11-03T12:00:00+00:00", 13),
        ("café", 1),
        ("日本語", 1),
        ("...", 3),
    ],
    ids=[
        "empty",
        "whitespace_only",
        "one_word",
        "two_words",
        "hyphenated",
        "underscore_is_a_word_character",
        "bin_and_code",
        "label_with_colon",
        "uuid",
        "iso_timestamp",
        "accented_word",
        "cjk_word",
        "three_punctuation_marks",
    ],
)
def test_count_tokens_counts_word_runs_and_single_punctuation_characters(text: str, expected: int) -> None:
    # Arrange / Act
    count = count_tokens(text)

    # Assert
    assert count == expected


def test_a_canonical_row_renders_to_the_specified_line_of_32_tokens() -> None:
    # Arrange
    row = _row(1, occurred_at=BASE_TIME)

    # Act
    line = render_row(row)

    # Assert
    assert line == (
        "00000000-0000-0000-0000-000000000001 2026-11-03T12:00:00+00:00 card declined 1050 USD "
        "bin=400000 code=insufficient_funds"
    )
    assert count_tokens(line) == ROW_TOKENS


def test_a_row_without_bin_and_code_renders_dashes_and_the_same_32_tokens() -> None:
    # Arrange
    row = _row(2, occurred_at=BASE_TIME, state="succeeded", decline_code=None, card_bin=None)

    # Act
    line = render_row(row)

    # Assert
    assert line == (
        "00000000-0000-0000-0000-000000000002 2026-11-03T12:00:00+00:00 card succeeded 1050 USD bin=- code=-"
    )
    assert count_tokens(line) == ROW_TOKENS


def test_the_naive_prompt_is_a_header_line_and_one_rendered_row_per_line() -> None:
    # Arrange
    rows = [_row(1), _row(2)]

    # Act
    prompt = naive_prompt(rows)

    # Assert
    assert prompt.splitlines() == ["Transactions:", render_row(rows[0]), render_row(rows[1])]
    assert count_tokens(prompt) == 2 + 2 * ROW_TOKENS


# --- chunk_rows --------------------------------------------------------------------------------------------


def test_chunk_rows_of_no_rows_is_an_empty_list() -> None:
    # Arrange / Act
    chunks = chunk_rows([], 2000)

    # Assert
    assert chunks == []


@pytest.mark.parametrize("limit", [0, -1, -2000])
def test_chunk_rows_rejects_a_non_positive_budget_with_value_error(limit: int) -> None:
    # Arrange
    rows = [_row(1)]

    # Act / Assert
    with pytest.raises(ValueError):
        chunk_rows(rows, limit)


def test_chunk_rows_packs_rows_greedily_in_order_up_to_the_budget() -> None:
    # Arrange: 64 tokens hold exactly two 32-token rows
    rows = [_row(number) for number in range(1, 6)]

    # Act
    chunks = chunk_rows(rows, 2 * ROW_TOKENS)

    # Assert
    assert _ids(chunks) == [[1, 2], [3, 4], [5]]


def test_chunk_rows_accepts_a_chunk_whose_tokens_equal_the_budget_exactly() -> None:
    # Arrange: 96 = three rows exactly; 95 fits only two
    rows = [_row(number) for number in range(1, 5)]

    # Act
    exact = chunk_rows(rows, 3 * ROW_TOKENS)
    one_short = chunk_rows(rows, 3 * ROW_TOKENS - 1)

    # Assert
    assert _ids(exact) == [[1, 2, 3], [4]]
    assert _ids(one_short) == [[1, 2], [3, 4]]


def test_chunk_rows_puts_a_single_oversized_row_in_its_own_chunk() -> None:
    # Arrange: the big row has a 59-token decline code, so it renders to 90 tokens, above the 70 budget
    big = _row(2, decline_code="-".join(["w"] * 30))
    rows = [_row(1), big, _row(3), _row(4)]
    assert count_tokens(render_row(big)) == 90

    # Act
    chunks = chunk_rows(rows, 70)

    # Assert
    assert _ids(chunks) == [[1], [2], [3, 4]]


def test_chunk_rows_gives_every_row_its_own_chunk_when_each_row_exceeds_the_budget() -> None:
    # Arrange
    rows = [_row(number) for number in range(1, 4)]

    # Act
    chunks = chunk_rows(rows, ROW_TOKENS - 1)

    # Assert
    assert _ids(chunks) == [[1], [2], [3]]


def test_chunk_rows_keeps_every_row_once_in_the_original_order() -> None:
    # Arrange
    rows = [_row(number) for number in (9, 3, 7, 1, 5, 8, 2)]

    # Act
    chunks = chunk_rows(rows, 100)

    # Assert
    assert [row for chunk in chunks for row in chunk] == rows


def test_every_multi_row_chunk_stays_within_the_token_budget() -> None:
    # Arrange
    rows = [_row(number) for number in range(1, 40)]
    budget = 200

    # Act
    chunks = chunk_rows(rows, budget)

    # Assert
    sizes = [sum(count_tokens(render_row(row)) for row in chunk) for chunk in chunks]
    assert len(chunks) == 7
    assert all(size <= budget for size in sizes)
    assert sizes == [192, 192, 192, 192, 192, 192, 96]


# --- summarize_chunk / merge_summaries ---------------------------------------------------------------------


def _mixed_rows() -> list[TxRow]:
    return [
        _row(1, state="succeeded", amount_minor=1000, decline_code=None),
        _row(2, state="declined", amount_minor=500, decline_code="insufficient_funds", card_bin="400000"),
        _row(
            3,
            state="declined",
            amount_minor=700,
            currency="EUR",
            decline_code="do_not_honor",
            card_bin="411111",
        ),
        _row(4, state="failed", amount_minor=300, decline_code="insufficient_funds", card_bin="400000"),
        _row(
            5,
            state="succeeded",
            amount_minor=200,
            currency="EUR",
            method="wallet",
            decline_code=None,
            card_bin=None,
        ),
        _row(6, state="succeeded", amount_minor=100, decline_code="ignored_code", card_bin="411111"),
        _row(7, state="declined", amount_minor=50, decline_code=None, card_bin=None),
    ]


def test_summarize_chunk_counts_amounts_states_methods_and_failure_details() -> None:
    # Arrange
    rows = _mixed_rows()

    # Act
    summary = summarize_chunk(rows)

    # Assert
    assert summary == ChunkSummary(
        rows=7,
        amount_by_currency={"USD": 1950, "EUR": 900},
        by_state={"succeeded": 3, "declined": 3, "failed": 1},
        by_method={"card": 6, "wallet": 1},
        decline_codes={"insufficient_funds": 2, "do_not_honor": 1},
        failed_by_bin={"400000": 2, "411111": 1},
    )


def test_summarize_chunk_ignores_decline_codes_and_bins_of_rows_that_did_not_fail() -> None:
    # Arrange
    rows = [_row(1, state="succeeded", decline_code="late_code", card_bin="555555")]

    # Act
    summary = summarize_chunk(rows)

    # Assert
    assert (summary.decline_codes, summary.failed_by_bin) == ({}, {})
    assert summary.by_state == {"succeeded": 1}


def test_summarize_chunk_of_no_rows_is_an_empty_summary() -> None:
    # Arrange / Act
    summary = summarize_chunk([])

    # Assert
    assert summary == ChunkSummary(
        rows=0, amount_by_currency={}, by_state={}, by_method={}, decline_codes={}, failed_by_bin={}
    )


def test_merging_the_summaries_of_the_chunks_equals_the_summary_of_all_rows() -> None:
    # Arrange
    rows = _mixed_rows()
    chunks = [rows[:3], rows[3:5], rows[5:]]

    # Act
    merged = merge_summaries([summarize_chunk(chunk) for chunk in chunks])

    # Assert
    assert merged == summarize_chunk(rows)


def test_merging_summaries_of_chunk_rows_output_equals_the_summary_of_all_rows() -> None:
    # Arrange
    rows = _mixed_rows()
    chunks = chunk_rows(rows, 2 * ROW_TOKENS)

    # Act
    merged = merge_summaries([summarize_chunk(chunk) for chunk in chunks])

    # Assert
    assert len(chunks) == 4
    assert merged == summarize_chunk(rows)


def test_merge_summaries_sums_counters_key_by_key_and_leaves_its_inputs_unchanged() -> None:
    # Arrange
    first = ChunkSummary(
        rows=2,
        amount_by_currency={"USD": 100},
        by_state={"declined": 2},
        by_method={"card": 2},
        decline_codes={"a": 2},
        failed_by_bin={"400000": 2},
    )
    second = ChunkSummary(
        rows=3,
        amount_by_currency={"USD": 50, "EUR": 7},
        by_state={"declined": 1, "succeeded": 2},
        by_method={"card": 1, "wallet": 2},
        decline_codes={"a": 1, "b": 4},
        failed_by_bin={"411111": 1},
    )

    # Act
    merged = merge_summaries([first, second])

    # Assert
    assert merged == ChunkSummary(
        rows=5,
        amount_by_currency={"USD": 150, "EUR": 7},
        by_state={"declined": 3, "succeeded": 2},
        by_method={"card": 3, "wallet": 2},
        decline_codes={"a": 3, "b": 4},
        failed_by_bin={"400000": 2, "411111": 1},
    )
    assert first.decline_codes == {"a": 2}
    assert second.amount_by_currency == {"USD": 50, "EUR": 7}


def test_merge_summaries_of_nothing_is_an_empty_summary() -> None:
    # Arrange / Act
    merged = merge_summaries([])

    # Assert
    assert merged == summarize_chunk([])


# --- render_summary ----------------------------------------------------------------------------------------


def _ranked_summary() -> ChunkSummary:
    return ChunkSummary(
        rows=120,
        amount_by_currency={"USD": 123456},
        by_state={"declined": 40, "succeeded": 80},
        by_method={"card": 100, "wallet": 20},
        decline_codes={
            "aaa_code": 1,
            "bbb_code": 2,
            "ccc_code": 4,
            "mmm_code": 3,
            "zzz_code": 3,
            "kkk_code": 2,
            "lll_code": 1,
        },
        failed_by_bin={
            "400001": 1,
            "400002": 2,
            "400003": 3,
            "400004": 4,
            "400005": 5,
            "400006": 6,
            "400007": 7,
        },
    )


def test_render_summary_keeps_the_top_items_by_count_descending_then_key_ascending() -> None:
    # Arrange
    summary = _ranked_summary()

    # Act
    rendered = render_summary(summary, top_items=3)

    # Assert: top 3 codes are ccc (4), mmm (3), zzz (3); top 3 bins are 400007, 400006, 400005
    assert rendered.index("ccc_code") < rendered.index("mmm_code") < rendered.index("zzz_code")
    assert rendered.index("400007") < rendered.index("400006") < rendered.index("400005")
    for dropped in ("aaa_code", "bbb_code", "kkk_code", "lll_code", "400004", "400003", "400002", "400001"):
        assert dropped not in rendered


def test_render_summary_breaks_a_count_tie_by_key_ascending_at_the_cut() -> None:
    # Arrange: bbb (2) and kkk (2) tie for the 4th place behind ccc, mmm, zzz
    summary = _ranked_summary()

    # Act
    rendered = render_summary(summary, top_items=4)

    # Assert
    assert "bbb_code" in rendered
    assert "kkk_code" not in rendered


def test_render_summary_defaults_to_the_top_five_items() -> None:
    # Arrange
    summary = _ranked_summary()

    # Act
    default = render_summary(summary)

    # Assert
    assert TOP_ITEMS == 5
    assert default == render_summary(summary, TOP_ITEMS)
    assert default == render_summary(summary, 5)
    assert "400003" in default
    assert "400002" not in default
    assert "bbb_code" in default
    assert "aaa_code" not in default


def test_render_summary_with_zero_top_items_leaves_out_both_top_lists_but_keeps_the_totals() -> None:
    # Arrange
    summary = _ranked_summary()

    # Act
    rendered = render_summary(summary, top_items=0)

    # Assert
    assert [code for code in summary.decline_codes if code in rendered] == []
    assert [bin_ for bin_ in summary.failed_by_bin if bin_ in rendered] == []
    for kept in ("120", "USD", "123456", "declined", "succeeded", "card", "wallet"):
        assert kept in rendered


def test_render_summary_costs_fewer_tokens_the_fewer_top_items_it_keeps() -> None:
    # Arrange
    summary = _ranked_summary()

    # Act
    sizes = [count_tokens(render_summary(summary, top_items=k)) for k in range(0, 8)]

    # Assert
    assert all(smaller < larger for smaller, larger in zip(sizes, sizes[1:], strict=False))


def test_render_summary_never_contains_a_payment_id() -> None:
    # Arrange
    rows = _mixed_rows()
    summary = summarize_chunk(rows)

    # Act
    rendered = render_summary(summary)

    # Assert
    assert [str(row.payment_id) for row in rows if str(row.payment_id) in rendered] == []
