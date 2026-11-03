import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.tools import (
    FAILED_STATES,
    BinShare,
    FailureAggregate,
    IncidentHit,
    aggregate_failures,
    card_payments_in_window,
    compare_baseline,
    search_incidents,
)
from ops_ai.scope import Scope

Intent = Literal["failure_spike", "unsupported"]
CauseLabel = Literal["issuer_outage_on_bin", "elevated_failures", "no_anomaly", "unsupported"]

WINDOW = timedelta(hours=6)
BASELINE_DAYS = 7
MIN_WINDOW_FAILURES = 10
PAYMENT_EVIDENCE_LIMIT = 5
FALLBACK_INCIDENT_QUERY = "card failures"


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, str]


@dataclass(frozen=True)
class Investigation:
    intent: Intent
    cause_label: CauseLabel
    window: FailureAggregate | None
    baseline: FailureAggregate | None
    suggested_fixes: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    evidence: tuple[EvidenceItem, ...]
    mode: AnswerMode
    text: str | None
    """None in t2_fallback."""
    model: str | None


UNSUPPORTED = Investigation(
    intent="unsupported",
    cause_label="unsupported",
    window=None,
    baseline=None,
    suggested_fixes=(),
    tool_calls=(),
    evidence=(),
    mode="t2_fallback",
    text=None,
    model=None,
)


def classify_intent(question: str) -> Intent:
    lowered = question.lower()
    about_failures = "fail" in lowered or "declin" in lowered
    about_payments = "card" in lowered or "payment" in lowered
    return "failure_spike" if about_failures and about_payments else "unsupported"


def investigate(session: Session, scope: Scope, question: str, now: datetime, llm: LlmPort) -> Investigation:
    """Tools first: the last 6 hours against the 7 days before, then incident search on the top failing BIN.

    The cause label and every number come from the tools; the LLM only narrates the evidence
    (LlmUnavailableError → t2_fallback with the same fields). Unsupported questions call no tool and no LLM.
    """
    bounded = question[:MAX_QUESTION_CHARS]
    if classify_intent(bounded) == "unsupported":
        return UNSUPPORTED
    window_start = now - WINDOW
    window = aggregate_failures(session, scope, window_start, now)
    baseline = compare_baseline(session, scope, window_start, days=BASELINE_DAYS)
    top_bin = window.by_bin[0] if window.by_bin else None
    query = f"BIN {top_bin.card_bin} issuer" if top_bin else FALLBACK_INCIDENT_QUERY
    incidents = search_incidents(session, scope, query)
    tool_calls = (
        ToolCall("aggregate_failures", {"start": window_start.isoformat(), "end": now.isoformat()}),
        ToolCall("compare_baseline", {"window_start": window_start.isoformat(), "days": str(BASELINE_DAYS)}),
        ToolCall("search_incidents", {"query": query}),
    )
    cause_label = _cause_label(window, baseline, top_bin)
    suggested_fixes = () if cause_label == "no_anomaly" else _unique_fixes(incidents)
    evidence = (
        _aggregate_evidence("aggregate:window", "Last 6 hours", window),
        _aggregate_evidence("aggregate:baseline", f"Baseline ({BASELINE_DAYS} days before)", baseline),
        *([_bin_evidence(window, top_bin)] if top_bin else []),
        *(
            EvidenceItem(ref=f"incident:{hit.incident_id}", summary=f"{hit.title}. Fix: {hit.fix_applied}")
            for hit in incidents
        ),
        *(_payment_evidence(session, scope, window, top_bin) if top_bin else []),
    )
    analysis = Investigation(
        intent="failure_spike",
        cause_label=cause_label,
        window=window,
        baseline=baseline,
        suggested_fixes=suggested_fixes,
        tool_calls=tool_calls,
        evidence=evidence,
        mode="t2_fallback",
        text=None,
        model=None,
    )
    try:
        response = llm.complete(LlmRequest(question=bounded, evidence=evidence))
    except LlmUnavailableError:
        return analysis
    return dataclasses.replace(analysis, mode="llm", text=response.text, model=response.model)


def _cause_label(
    window: FailureAggregate, baseline: FailureAggregate, top_bin: BinShare | None
) -> CauseLabel:
    # window.rate >= 2 * baseline.rate, cross-multiplied so equality is exact
    spiking = window.failed * baseline.total >= 2 * baseline.failed * window.total
    if not spiking or window.failed < MIN_WINDOW_FAILURES:
        return "no_anomaly"
    if top_bin is not None and 2 * top_bin.failed >= window.failed:
        return "issuer_outage_on_bin"
    return "elevated_failures"


def _unique_fixes(incidents: list[IncidentHit]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(hit.fix_applied for hit in incidents))


def _percent(numerator: int, denominator: int) -> str:
    return f"{(numerator / denominator if denominator else 0.0) * 100:.1f}%"


def _aggregate_evidence(ref: str, label: str, aggregate: FailureAggregate) -> EvidenceItem:
    return EvidenceItem(
        ref=ref,
        summary=(
            f"{label}: {aggregate.failed} of {aggregate.total} card payments failed "
            f"({_percent(aggregate.failed, aggregate.total)}), "
            f"{aggregate.start.isoformat()} to {aggregate.end.isoformat()}"
        ),
    )


def _bin_evidence(window: FailureAggregate, top_bin: BinShare) -> EvidenceItem:
    return EvidenceItem(
        ref=f"aggregate:bin:{top_bin.card_bin}",
        summary=(
            f"BIN {top_bin.card_bin}: {top_bin.failed} failed of {top_bin.total} card payments, "
            f"{_percent(top_bin.failed, window.failed)} of the window failures"
        ),
    )


def _payment_evidence(
    session: Session, scope: Scope, window: FailureAggregate, top_bin: BinShare
) -> list[EvidenceItem]:
    """The newest failed card payments on the top BIN inside the window."""
    rows = session.execute(
        select(PaymentFact.payment_id, PaymentFact.decline_code, PaymentFact.occurred_at)
        .where(
            *card_payments_in_window(scope, window.start, window.end),
            PaymentFact.state.in_(FAILED_STATES),
            PaymentFact.card_bin == top_bin.card_bin,
        )
        .order_by(PaymentFact.occurred_at.desc(), PaymentFact.payment_id)
        .limit(PAYMENT_EVIDENCE_LIMIT)
    ).all()
    return [
        EvidenceItem(
            ref=f"payment:{row.payment_id}",
            summary=(
                f"Card payment failed on BIN {top_bin.card_bin} "
                f"({row.decline_code or 'no decline code'}) at {row.occurred_at.isoformat()}"
            ),
        )
        for row in rows
    ]
