from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.models import IncidentRecord
from ops_ai.scope import Scope

FAILED_STATES = ("declined", "failed")
CARD_METHOD = "card"


@dataclass(frozen=True)
class BinShare:
    card_bin: str
    failed: int
    total: int


@dataclass(frozen=True)
class CodeCount:
    decline_code: str
    count: int


@dataclass(frozen=True)
class FailureAggregate:
    start: datetime
    end: datetime
    total: int
    failed: int
    rate: float
    """failed / total, 0.0 when total is 0."""
    by_bin: tuple[BinShare, ...]
    """Bins with at least one failure, most failures first; NULL bins left out."""
    by_code: tuple[CodeCount, ...]


@dataclass(frozen=True)
class IncidentHit:
    incident_id: str
    title: str
    root_cause: str
    fix_applied: str
    resolved_at: datetime | None
    recurrence_within_7d: bool


def card_payments_in_window(scope: Scope, start: datetime, end: datetime) -> list[ColumnElement[bool]]:
    """`start <= occurred_at < end`, card only, within scope."""
    return [
        PaymentFact.merchant_id == scope.merchant_id,
        PaymentFact.method == CARD_METHOD,
        PaymentFact.occurred_at >= start,
        PaymentFact.occurred_at < end,
    ]


def aggregate_failures(session: Session, scope: Scope, start: datetime, end: datetime) -> FailureAggregate:
    """Card payment totals and failures (declined or failed) in [start, end), by BIN and decline code."""
    window = card_payments_in_window(scope, start, end)
    is_failed = PaymentFact.state.in_(FAILED_STATES)
    failed_count = func.count().filter(is_failed)
    totals = session.execute(select(func.count(), failed_count).where(*window)).one()
    total, failed = int(totals[0]), int(totals[1])
    bin_failed = failed_count.label("failed")
    bins = session.execute(
        select(PaymentFact.card_bin, bin_failed, func.count().label("total"))
        .where(*window, PaymentFact.card_bin.is_not(None))
        .group_by(PaymentFact.card_bin)
        .having(failed_count > 0)
        .order_by(bin_failed.desc(), PaymentFact.card_bin)
    ).all()
    code_count = func.count().label("count")
    codes = session.execute(
        select(PaymentFact.decline_code, code_count)
        .where(*window, is_failed, PaymentFact.decline_code.is_not(None))
        .group_by(PaymentFact.decline_code)
        .order_by(code_count.desc(), PaymentFact.decline_code)
    ).all()
    return FailureAggregate(
        start=start,
        end=end,
        total=total,
        failed=failed,
        rate=failed / total if total else 0.0,
        by_bin=tuple(
            BinShare(card_bin=str(row.card_bin), failed=row.failed, total=row.total) for row in bins
        ),
        by_code=tuple(CodeCount(decline_code=str(code), count=count) for code, count in codes),
    )


def compare_baseline(
    session: Session, scope: Scope, window_start: datetime, days: int = 7
) -> FailureAggregate:
    """aggregate_failures over the `days` before `window_start`."""
    return aggregate_failures(session, scope, window_start - timedelta(days=days), window_start)


def search_incidents(session: Session, scope: Scope, query: str, limit: int = 5) -> list[IncidentHit]:
    """Full-text search (websearch_to_tsquery, english) over the merchant's and platform-wide incidents.

    Resolved incidents first, then best rank.
    """
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(IncidentRecord.tsv, tsquery)
    rows = session.execute(
        select(
            IncidentRecord.incident_id,
            IncidentRecord.title,
            IncidentRecord.root_cause,
            IncidentRecord.fix_applied,
            IncidentRecord.resolved_at,
            IncidentRecord.recurrence_within_7d,
        )
        .where(
            or_(IncidentRecord.merchant_id == scope.merchant_id, IncidentRecord.merchant_id.is_(None)),
            IncidentRecord.tsv.op("@@")(tsquery),
        )
        .order_by(IncidentRecord.resolved_at.is_(None), rank.desc(), IncidentRecord.incident_id)
        .limit(limit)
    ).all()
    return [
        IncidentHit(
            incident_id=row.incident_id,
            title=row.title,
            root_cause=row.root_cause,
            fix_applied=row.fix_applied,
            resolved_at=row.resolved_at,
            recurrence_within_7d=row.recurrence_within_7d,
        )
        for row in rows
    ]


MIN_ELEVATED_FAILURES = 10
MAX_TIMELINE_BUCKETS = 96
ChangeKind = Literal["failure_onset", "new_decline_code", "bin_onset", "incident_started"]


@dataclass(frozen=True)
class BaselineComparison:
    window: FailureAggregate
    baseline: FailureAggregate
    ratio: float | None
    elevated: bool


@dataclass(frozen=True)
class TimelineBucket:
    start: datetime
    end: datetime
    total: int
    failed: int
    rate: float


@dataclass(frozen=True)
class ChangeEvent:
    at: datetime
    kind: ChangeKind
    subject: str
    detail: str


@dataclass(frozen=True)
class ChangeTimeline:
    buckets: tuple[TimelineBucket, ...]
    changes: tuple[ChangeEvent, ...]


def _twice_baseline(failed: int, total: int, baseline_failed: int, baseline_total: int) -> bool:
    return failed * baseline_total >= 2 * baseline_failed * total


def compare_to_baseline(
    session: Session, scope: Scope, start: datetime, end: datetime, baseline_days: int = 7
) -> BaselineComparison:
    window = aggregate_failures(session, scope, start, end)
    baseline = compare_baseline(session, scope, start, days=baseline_days)
    return BaselineComparison(
        window=window,
        baseline=baseline,
        ratio=window.rate / baseline.rate if baseline.rate else None,
        elevated=window.failed >= MIN_ELEVATED_FAILURES
        and _twice_baseline(window.failed, window.total, baseline.failed, baseline.total),
    )


def change_timeline(
    session: Session,
    scope: Scope,
    start: datetime,
    end: datetime,
    bucket_minutes: int = 60,
    baseline_days: int = 7,
) -> ChangeTimeline:
    if start >= end or bucket_minutes <= 0:
        raise ValueError("Invalid timeline window or bucket size")
    step = timedelta(minutes=bucket_minutes)
    if end - start > MAX_TIMELINE_BUCKETS * step:
        raise ValueError("Too many timeline buckets")
    baseline_start = start - timedelta(days=baseline_days)
    baseline = aggregate_failures(session, scope, baseline_start, start)
    buckets: list[TimelineBucket] = []
    changes: list[ChangeEvent] = []
    cursor = start
    onset_found = False
    while cursor < end:
        bucket_end = min(cursor + step, end)
        aggregate = aggregate_failures(session, scope, cursor, bucket_end)
        buckets.append(TimelineBucket(cursor, bucket_end, aggregate.total, aggregate.failed, aggregate.rate))
        if (
            not onset_found
            and aggregate.failed >= 3
            and _twice_baseline(aggregate.failed, aggregate.total, baseline.failed, baseline.total)
        ):
            changes.append(
                ChangeEvent(
                    cursor,
                    "failure_onset",
                    "card",
                    f"{aggregate.failed} of {aggregate.total} card payments failed in this bucket.",
                )
            )
            onset_found = True
        cursor = bucket_end
    failed = PaymentFact.state.in_(FAILED_STATES)
    window = card_payments_in_window(scope, start, end)
    baseline_window = card_payments_in_window(scope, baseline_start, start)
    baseline_codes = set(
        session.scalars(
            select(PaymentFact.decline_code).where(
                *baseline_window, failed, PaymentFact.decline_code.is_not(None)
            )
        )
    )
    codes = session.execute(
        select(PaymentFact.decline_code, func.min(PaymentFact.occurred_at), func.count())
        .where(*window, failed, PaymentFact.decline_code.is_not(None))
        .group_by(PaymentFact.decline_code)
    )
    for code, first_at, count in codes:
        if code not in baseline_codes:
            changes.append(
                ChangeEvent(
                    first_at, "new_decline_code", str(code), f"New decline code {code}: {count} failures."
                )
            )
    baseline_bins = {
        str(card_bin): (int(failure_count), int(total))
        for card_bin, failure_count, total in session.execute(
            select(PaymentFact.card_bin, func.count().filter(failed), func.count())
            .where(*baseline_window, PaymentFact.card_bin.is_not(None))
            .group_by(PaymentFact.card_bin)
        )
    }
    bins = session.execute(
        select(
            PaymentFact.card_bin,
            func.count().filter(failed),
            func.count(),
            func.min(PaymentFact.occurred_at).filter(failed),
        )
        .where(*window, PaymentFact.card_bin.is_not(None))
        .group_by(PaymentFact.card_bin)
    )
    for card_bin, failure_count, total, first_at in bins:
        baseline_failed, baseline_total = baseline_bins.get(str(card_bin), (0, 0))
        if failure_count >= 3 and _twice_baseline(failure_count, total, baseline_failed, baseline_total):
            changes.append(
                ChangeEvent(
                    first_at,
                    "bin_onset",
                    str(card_bin),
                    f"BIN {card_bin}: {failure_count} of {total} payments failed.",
                )
            )
    incidents = session.scalars(
        select(IncidentRecord).where(
            or_(IncidentRecord.merchant_id == scope.merchant_id, IncidentRecord.merchant_id.is_(None)),
            IncidentRecord.started_at >= start,
            IncidentRecord.started_at < end,
        )
    )
    changes.extend(
        ChangeEvent(hit.started_at, "incident_started", hit.incident_id, f"Incident started: {hit.title}.")
        for hit in incidents
    )
    changes.sort(key=lambda change: (change.at, change.kind, change.subject))
    return ChangeTimeline(tuple(buckets), tuple(changes))
