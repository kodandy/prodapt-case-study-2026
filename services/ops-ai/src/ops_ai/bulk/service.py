"""Bulk analysis of a merchant's payments over a window: the LLM sees the budgeted summary, never the raw
rows."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.bulk.analysis import BulkAnalysis, TxRow, analyze_bulk
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope

MAX_BULK_ROWS = 20000
BULK_QUESTION = (
    "Summarise the notable patterns in this merchant's transactions (failure concentrations by decline code, "
    "BIN, method or currency) using only the evidence."
)


@dataclass(frozen=True)
class BulkResult:
    analysis: BulkAnalysis
    mode: Literal["llm", "t2_fallback"]
    text: str | None
    model: str | None


def load_rows(session: Session, scope: Scope, start: datetime, end: datetime) -> list[TxRow]:
    facts = session.execute(
        select(PaymentFact)
        .where(
            PaymentFact.merchant_id == scope.merchant_id,
            PaymentFact.occurred_at >= start,
            PaymentFact.occurred_at < end,
        )
        .order_by(PaymentFact.occurred_at, PaymentFact.payment_id)
        .limit(MAX_BULK_ROWS)
    ).scalars()
    return [
        TxRow(
            payment_id=fact.payment_id,
            amount_minor=fact.amount_minor,
            currency=fact.currency,
            method=fact.method,
            state=fact.state,
            decline_code=fact.decline_code,
            card_bin=fact.card_bin,
            occurred_at=fact.occurred_at,
        )
        for fact in facts
    ]


def narrate_bulk(analysis: BulkAnalysis, llm: LlmPort) -> BulkResult:
    """The LLM sees only the budgeted summary; LlmUnavailableError -> t2_fallback."""
    request = LlmRequest(
        question=BULK_QUESTION, evidence=(EvidenceItem(ref="bulk:summary", summary=analysis.prompt),)
    )
    try:
        response = llm.complete(request)
    except LlmUnavailableError:
        return BulkResult(analysis=analysis, mode="t2_fallback", text=None, model=None)
    return BulkResult(analysis=analysis, mode="llm", text=response.text, model=response.model)


def analyze_transactions(
    session: Session, scope: Scope, start: datetime, end: datetime, llm: LlmPort, token_budget: int
) -> BulkResult:
    return narrate_bulk(analyze_bulk(load_rows(session, scope, start, end), token_budget=token_budget), llm)
