from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import ABSTAIN_TEXT, Claim, ground_claims, grounded_narration
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import AnswerMode
from ops_ai.assistant.tool_calling import ToolCallingLlm
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.diagnosis import Diagnosis, diagnose
from ops_ai.investigation.playbook import Playbook
from ops_ai.investigation.query import FailureQuery, parse_failure_query
from ops_ai.investigation.reasons import ReasonBreakdown, failure_reason_groups
from ops_ai.investigation.service import ToolCall, _aggregate_evidence, _bin_evidence
from ops_ai.investigation.tools import (
    FAILED_STATES,
    BaselineComparison,
    ChangeTimeline,
    change_timeline,
    compare_to_baseline,
    search_incidents,
)
from ops_ai.scope import Scope


@dataclass(frozen=True)
class FailureAnswer:
    query: FailureQuery
    comparison: BaselineComparison
    timeline: ChangeTimeline
    breakdown: ReasonBreakdown
    diagnosis: Diagnosis
    claims: tuple[Claim, ...]
    abstained: bool
    tool_calls: tuple[ToolCall, ...]
    evidence: tuple[EvidenceItem, ...]
    mode: AnswerMode
    text: str | None
    model: str | None


def _payment_evidence(session: Session, query: FailureQuery) -> tuple[EvidenceItem, ...]:
    statement = select(PaymentFact).where(
        PaymentFact.merchant_id == query.merchant_id,
        PaymentFact.occurred_at >= query.start,
        PaymentFact.occurred_at < query.end,
        PaymentFact.state.in_((query.status,) if query.status else FAILED_STATES),
    )
    if query.method is not None:
        statement = statement.where(PaymentFact.method == query.method)
    if query.currency is not None:
        statement = statement.where(PaymentFact.currency == query.currency)
    rows = session.scalars(
        statement.order_by(PaymentFact.occurred_at.desc(), PaymentFact.payment_id).limit(5)
    )
    return tuple(
        EvidenceItem(
            ref=f"payment:{row.payment_id}",
            summary=f"{row.method} payment {row.state} ({row.decline_code or 'no code'}) "
            f"on BIN {row.card_bin or 'unknown'} at {row.occurred_at.isoformat()}",
        )
        for row in rows
    )


def investigate_failures(
    session: Session,
    scope: Scope,
    question: str,
    now: datetime,
    llm: LlmPort,
    tool_llm: ToolCallingLlm | None,
    playbook: Playbook,
) -> FailureAnswer:
    bounded = question[:2000]
    query = parse_failure_query(bounded, scope, now, tool_llm)
    comparison = compare_to_baseline(session, scope, query.start, query.end)
    duration_minutes = (query.end - query.start).total_seconds() / 60
    bucket_minutes = (
        60
        if duration_minutes <= 1440
        else next(size for size in (120, 360, 1440) if duration_minutes <= 96 * size)
    )
    timeline = change_timeline(session, scope, query.start, query.end, bucket_minutes=bucket_minutes)
    breakdown = failure_reason_groups(session, query, tool_llm)
    top_bin = comparison.window.by_bin[0] if comparison.window.by_bin else None
    top_words = breakdown.groups[0].taxonomy_code.replace("_", " ") if breakdown.groups else "card failures"
    incident_query = f"BIN {top_bin.card_bin} {top_words}" if top_bin else f"{top_words} card failures"
    incidents = search_incidents(session, scope, incident_query)
    payments = _payment_evidence(session, query)
    bounds = {"start": query.start.isoformat(), "end": query.end.isoformat()}
    filters = {
        name: value
        for name, value in (
            ("method", query.method),
            ("status", query.status),
            ("country", query.country),
            ("currency", query.currency),
        )
        if value is not None
    }
    tool_calls = (
        ToolCall("compare_baseline", {**bounds, "baseline_days": "7"}),
        ToolCall("change_timeline", {**bounds, "bucket_minutes": str(bucket_minutes)}),
        ToolCall("failure_reasons", {**bounds, **filters}),
        ToolCall("search_incidents", {"query": incident_query}),
        ToolCall("list_failed_payments", {**bounds, "limit": "5"}),
    )
    evidence = (
        _aggregate_evidence("aggregate:window", "Window", comparison.window),
        _aggregate_evidence("aggregate:baseline", "Baseline", comparison.baseline),
        EvidenceItem(
            ref="comparison:baseline",
            summary=(
                f"Window rate {comparison.window.rate:.1%}; baseline rate {comparison.baseline.rate:.1%}; "
                f"elevated: {comparison.elevated}."
            ),
        ),
        *([_bin_evidence(comparison.window, top_bin)] if top_bin else []),
        *(
            EvidenceItem(ref=f"change:{change.kind}:{change.subject}", summary=change.detail)
            for change in timeline.changes
        ),
        *(
            EvidenceItem(
                ref=f"reason:{group.taxonomy_code}",
                summary=(
                    f"{group.count} failures ({group.share:.1%}) are {group.taxonomy_code}; "
                    f"owner {group.owner}; retryable {'yes' if group.retryable else 'no'}; "
                    f"raw codes: {', '.join(group.raw_codes)}."
                ),
            )
            for group in breakdown.groups
        ),
        *(
            EvidenceItem(ref=f"incident:{hit.incident_id}", summary=f"{hit.title}. Fix: {hit.fix_applied}")
            for hit in incidents
        ),
        *payments,
    )
    diagnosis = diagnose(comparison, timeline, breakdown, incidents, playbook)
    claims = ground_claims(
        (
            *diagnosis.facts,
            *diagnosis.hypotheses,
            *diagnosis.how_to_confirm,
            *(
                Claim(f"Fix {fix.rank}: {fix.fix}", (*fix.evidence_refs, *fix.incident_refs))
                for fix in diagnosis.fixes
            ),
        ),
        evidence,
    )
    abstained = comparison.window.total == 0 or not claims
    answer = FailureAnswer(
        query,
        comparison,
        timeline,
        breakdown,
        diagnosis,
        () if abstained else claims,
        abstained,
        tool_calls,
        evidence,
        "t2_fallback",
        ABSTAIN_TEXT if abstained else None,
        None,
    )
    session.close()
    if abstained:
        return answer
    try:
        response = llm.complete(LlmRequest(question=bounded, evidence=evidence))
    except LlmUnavailableError:
        return answer
    narration = grounded_narration(response.text, evidence)
    if narration is None:
        return answer
    return replace(answer, mode="llm", text=narration, model=response.model)
