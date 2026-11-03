from dataclasses import dataclass
from datetime import datetime, timedelta

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
