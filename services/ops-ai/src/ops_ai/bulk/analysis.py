"""Bulk transaction analysis under a token budget (R2-5): chunk, summarise each chunk (map), merge
(reduce), then render the smallest useful prompt. Payment ids never leave the map step; only counters
and totals do.
"""

import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from ops_ai.bulk.tokens import count_tokens

DEFAULT_TOKEN_BUDGET = 800
DEFAULT_CHUNK_TOKENS = 2000
TOP_ITEMS = 5
FAILED_STATES = frozenset({"declined", "failed"})


@dataclass(frozen=True)
class TxRow:
    payment_id: uuid.UUID
    amount_minor: int
    currency: str
    method: str
    state: str
    decline_code: str | None
    card_bin: str | None
    occurred_at: datetime


@dataclass(frozen=True)
class ChunkSummary:
    rows: int
    amount_by_currency: dict[str, int]
    by_state: dict[str, int]
    by_method: dict[str, int]
    decline_codes: dict[str, int]
    failed_by_bin: dict[str, int]


@dataclass(frozen=True)
class BulkAnalysis:
    transactions: int
    chunks: int
    naive_tokens: int
    optimized_tokens: int
    reduction: float
    within_budget: bool
    summary: ChunkSummary
    prompt: str


def render_row(row: TxRow) -> str:
    return (
        f"{row.payment_id} {row.occurred_at.isoformat()} {row.method} {row.state} {row.amount_minor} "
        f"{row.currency} bin={row.card_bin or '-'} code={row.decline_code or '-'}"
    )


def naive_prompt(rows: Sequence[TxRow]) -> str:
    return "Transactions:\n" + "\n".join(render_row(row) for row in rows)


def chunk_rows(rows: Sequence[TxRow], max_tokens_per_chunk: int) -> list[list[TxRow]]:
    if max_tokens_per_chunk <= 0:
        raise ValueError("max_tokens_per_chunk must be positive")
    chunks: list[list[TxRow]] = []
    current: list[TxRow] = []
    used = 0
    for row in rows:
        tokens = count_tokens(render_row(row))
        if current and used + tokens > max_tokens_per_chunk:
            chunks.append(current)
            current, used = [], 0
        current.append(row)
        used += tokens
    if current:
        chunks.append(current)
    return chunks


def summarize_chunk(rows: Sequence[TxRow]) -> ChunkSummary:
    amounts: Counter[str] = Counter()
    states: Counter[str] = Counter()
    methods: Counter[str] = Counter()
    codes: Counter[str] = Counter()
    bins: Counter[str] = Counter()
    for row in rows:
        amounts[row.currency] += row.amount_minor
        states[row.state] += 1
        methods[row.method] += 1
        if row.state in FAILED_STATES:
            if row.decline_code:
                codes[row.decline_code] += 1
            if row.card_bin:
                bins[row.card_bin] += 1
    return ChunkSummary(
        rows=len(rows),
        amount_by_currency=dict(amounts),
        by_state=dict(states),
        by_method=dict(methods),
        decline_codes=dict(codes),
        failed_by_bin=dict(bins),
    )


def _sum(maps: Sequence[Mapping[str, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for counts in maps:
        for key, value in counts.items():
            total[key] = total.get(key, 0) + value
    return total


def merge_summaries(summaries: Sequence[ChunkSummary]) -> ChunkSummary:
    return ChunkSummary(
        rows=sum(summary.rows for summary in summaries),
        amount_by_currency=_sum([summary.amount_by_currency for summary in summaries]),
        by_state=_sum([summary.by_state for summary in summaries]),
        by_method=_sum([summary.by_method for summary in summaries]),
        decline_codes=_sum([summary.decline_codes for summary in summaries]),
        failed_by_bin=_sum([summary.failed_by_bin for summary in summaries]),
    )


def _sorted_items(counts: Mapping[str, int]) -> list[tuple[str, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _line(label: str, counts: Mapping[str, int]) -> str:
    return f"{label}: " + ", ".join(f"{key}={value}" for key, value in _sorted_items(counts))


def render_summary(summary: ChunkSummary, top_items: int = TOP_ITEMS) -> str:
    lines = [
        f"rows: {summary.rows}",
        _line("amount_minor by currency", summary.amount_by_currency),
        _line("by state", summary.by_state),
        _line("by method", summary.by_method),
    ]
    if top_items > 0:
        codes = dict(_sorted_items(summary.decline_codes)[:top_items])
        bins = dict(_sorted_items(summary.failed_by_bin)[:top_items])
        lines.append(_line(f"top {top_items} decline codes", codes))
        lines.append(_line(f"top {top_items} failing BINs", bins))
    return "\n".join(lines)


def analyze_bulk(
    rows: Sequence[TxRow], token_budget: int = DEFAULT_TOKEN_BUDGET, chunk_tokens: int = DEFAULT_CHUNK_TOKENS
) -> BulkAnalysis:
    chunks = chunk_rows(rows, chunk_tokens)
    merged = merge_summaries([summarize_chunk(chunk) for chunk in chunks])
    naive_tokens = count_tokens(naive_prompt(rows))
    prompt = render_summary(merged, 0)
    for top_items in range(TOP_ITEMS, 0, -1):
        candidate = render_summary(merged, top_items)
        if count_tokens(candidate) <= token_budget:
            prompt = candidate
            break
    optimized_tokens = count_tokens(prompt)
    return BulkAnalysis(
        transactions=len(rows),
        chunks=len(chunks),
        naive_tokens=naive_tokens,
        optimized_tokens=optimized_tokens,
        reduction=0.0 if not rows or naive_tokens == 0 else 1 - optimized_tokens / naive_tokens,
        within_budget=optimized_tokens <= token_budget,
        summary=merged,
        prompt=prompt,
    )
