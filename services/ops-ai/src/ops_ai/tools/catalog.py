import uuid
from datetime import timedelta
from typing import Literal

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import Claim
from ops_ai.fraud_watch.analysis import analyze_fraud
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.query import FailureQueryArgs, to_failure_query
from ops_ai.investigation.reasons import ReasonBreakdown, failure_reason_groups, transfer_reason_groups
from ops_ai.investigation.tools import (
    FAILED_STATES,
    aggregate_failures,
    change_timeline,
    compare_to_baseline,
    search_incidents,
)
from ops_ai.scope import Scope, UserScope
from ops_ai.tools.registry import ToolContext, ToolDefinition, ToolOutput, ToolRegistry
from ops_ai.transfers.tools import get_payment_status, sender_facing_status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PaymentArgs(Args):
    payment_id: uuid.UUID


class ListArgs(FailureQueryArgs):
    limit: int = Field(default=20, ge=1, le=50)


class AggregateArgs(Args):
    lookback_hours: int = Field(default=6, ge=1, le=720)
    end_hours_ago: int = Field(default=0, ge=0, le=720)


class BaselineArgs(Args):
    window_hours: int = Field(default=6, ge=1, le=72)
    baseline_days: int = Field(default=7, ge=1, le=30)


class TimelineArgs(BaselineArgs):
    bucket_minutes: Literal[15, 30, 60] = 60


class IncidentArgs(Args):
    query: str = Field(min_length=1, max_length=200)


class FraudArgs(Args):
    lookback_hours: int = Field(default=24, ge=1, le=168)


class TransferArgs(Args):
    transfer_id: uuid.UUID | None = None


class TransferReasonsArgs(Args):
    lookback_hours: int = Field(default=720, ge=1, le=720)


def _output(evidence: list[EvidenceItem]) -> ToolOutput:
    return ToolOutput(tuple(evidence), tuple(Claim(item.summary, (item.ref,)) for item in evidence))


def _payment_item(payment: PaymentFact) -> EvidenceItem:
    return EvidenceItem(
        ref=f"payment:{payment.payment_id}",
        summary=(
            f"Payment {payment.state}, decline code {payment.decline_code or 'none'}, "
            f"amount {payment.amount_minor} {payment.currency}, at {payment.occurred_at.isoformat()}"
        ),
    )


def _payment(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = PaymentArgs.model_validate(args.model_dump())
    fact = context.session.scalar(
        select(PaymentFact).where(
            PaymentFact.merchant_id == context.scope.merchant_id, PaymentFact.payment_id == parsed.payment_id
        )
    )
    return _output([_payment_item(fact)] if fact is not None else [])


def _list(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = ListArgs.model_validate(args.model_dump())
    query = to_failure_query(parsed, context.scope, context.now, "rules")
    filters = [
        PaymentFact.merchant_id == context.scope.merchant_id,
        PaymentFact.occurred_at >= query.start,
        PaymentFact.occurred_at < query.end,
        PaymentFact.state.in_((query.status,) if query.status else FAILED_STATES),
    ]
    if query.method is not None:
        filters.append(PaymentFact.method == query.method)
    if query.currency is not None:
        filters.append(PaymentFact.currency == query.currency)
    count = context.session.scalar(select(func.count()).select_from(PaymentFact).where(*filters)) or 0
    facts = context.session.scalars(
        select(PaymentFact)
        .where(*filters)
        .order_by(PaymentFact.occurred_at.desc(), PaymentFact.payment_id)
        .limit(parsed.limit)
    ).all()
    aggregate = EvidenceItem("aggregate:failed_payments", f"{count} failed payments in the requested window")
    return ToolOutput(
        (aggregate, *(_payment_item(fact) for fact in facts)), (Claim(aggregate.summary, (aggregate.ref,)),)
    )


def _aggregate(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = AggregateArgs.model_validate(args.model_dump())
    end = context.now - timedelta(hours=parsed.end_hours_ago)
    aggregate = aggregate_failures(
        context.session, context.scope, end - timedelta(hours=parsed.lookback_hours), end
    )
    summary = f"{aggregate.failed} failures of {aggregate.total} card payments"
    return _output([EvidenceItem("aggregate:window", summary)])


def _baseline(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = BaselineArgs.model_validate(args.model_dump())
    comparison = compare_to_baseline(
        context.session,
        context.scope,
        context.now - timedelta(hours=parsed.window_hours),
        context.now,
        baseline_days=parsed.baseline_days,
    )
    evidence = (
        EvidenceItem(
            "aggregate:window",
            (f"{comparison.window.failed} failures of {comparison.window.total} card payments"),
        ),
        EvidenceItem(
            "aggregate:baseline",
            (f"{comparison.baseline.failed} failures of {comparison.baseline.total} baseline card payments"),
        ),
        EvidenceItem("comparison:baseline", f"Failure rate elevated: {comparison.elevated}"),
    )
    return ToolOutput(evidence, (Claim(evidence[2].summary, tuple(item.ref for item in evidence)),))


def _timeline(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = TimelineArgs.model_validate(args.model_dump())
    timeline = change_timeline(
        context.session,
        context.scope,
        context.now - timedelta(hours=parsed.window_hours),
        context.now,
        bucket_minutes=parsed.bucket_minutes,
        baseline_days=parsed.baseline_days,
    )
    changes = [
        EvidenceItem(
            f"change:{change.kind}:{change.subject}",
            f"{change.kind} {change.subject} at {change.at.isoformat()}",
        )
        for change in timeline.changes
    ]
    buckets = [
        EvidenceItem(
            f"timeline:{bucket.start.isoformat()}",
            f"{bucket.failed} failures of {bucket.total} card payments",
        )
        for bucket in timeline.buckets
        if bucket.failed
    ]
    output = _output(changes)
    return ToolOutput(output.evidence + tuple(buckets), output.claims)


def _reason_output(breakdown: ReasonBreakdown) -> ToolOutput:
    return _output(
        [
            EvidenceItem(
                f"reason:{group.taxonomy_code}",
                f"{group.taxonomy_code}: {group.count} failures, share {group.share:.1%}, "
                f"owner {group.owner}, retryable {group.retryable}",
            )
            for group in breakdown.groups
        ]
    )


def _reasons(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = FailureQueryArgs.model_validate(args.model_dump())
    query = to_failure_query(parsed, context.scope, context.now, "rules")
    return _reason_output(failure_reason_groups(context.session, query, None))


def _incidents(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = IncidentArgs.model_validate(args.model_dump())
    return _output(
        [
            EvidenceItem(
                f"incident:{hit.incident_id}", f"{hit.title}: {hit.root_cause}; fix: {hit.fix_applied}"
            )
            for hit in search_incidents(context.session, context.scope, parsed.query)
        ]
    )


def _fraud(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
    parsed = FraudArgs.model_validate(args.model_dump())
    analysis = analyze_fraud(
        context.session, context.scope, context.now - timedelta(hours=parsed.lookback_hours), context.now
    )
    return _output(
        [
            EvidenceItem(
                f"episode:{episode.first_payment_id}",
                f"Suspected card testing: {episode.micro_payments} micro payments, "
                f"decline ratio {episode.decline_ratio:.1%}",
            )
            for episode in analysis.episodes
            if episode.flagged
        ]
    )


def _transfer(context: ToolContext[UserScope], args: BaseModel) -> ToolOutput:
    parsed = TransferArgs.model_validate(args.model_dump())
    transfer = get_payment_status(context.session, context.scope, parsed.transfer_id)
    if transfer is None:
        return _output([])
    return _output(
        [
            EvidenceItem(
                f"transfer:{transfer.transfer_id}",
                f"Transfer {sender_facing_status(transfer.status)}, "
                f"amount {transfer.amount_minor} {transfer.currency}, "
                f"at {transfer.decided_at.isoformat()}",
            )
        ]
    )


def _transfer_reasons(context: ToolContext[UserScope], args: BaseModel) -> ToolOutput:
    parsed = TransferReasonsArgs.model_validate(args.model_dump())
    return _reason_output(
        transfer_reason_groups(
            context.session,
            context.scope,
            context.now - timedelta(hours=parsed.lookback_hours),
            context.now,
            None,
        )
    )


def merchant_tools() -> ToolRegistry[Scope]:
    return ToolRegistry(
        [
            ToolDefinition("get_payment_status", "Get a payment's recorded status.", PaymentArgs, _payment),
            ToolDefinition("list_failed_payments", "List recent failed payments.", ListArgs, _list),
            ToolDefinition("aggregate_failures", "Count card payment failures.", AggregateArgs, _aggregate),
            ToolDefinition(
                "compare_baseline", "Compare failures with their baseline.", BaselineArgs, _baseline
            ),
            ToolDefinition("change_timeline", "Find changes in card failures.", TimelineArgs, _timeline),
            ToolDefinition("failure_reasons", "Group payment failure reasons.", FailureQueryArgs, _reasons),
            ToolDefinition("search_incidents", "Search past incidents.", IncidentArgs, _incidents),
            ToolDefinition("analyze_fraud", "Analyze suspected card testing.", FraudArgs, _fraud),
        ]
    )


def user_tools() -> ToolRegistry[UserScope]:
    return ToolRegistry(
        [
            ToolDefinition(
                "get_payment_status", "Get a transfer's sender-facing status.", TransferArgs, _transfer
            ),
            ToolDefinition(
                "failure_reasons", "Group failed transfer reasons.", TransferReasonsArgs, _transfer_reasons
            ),
        ]
    )
