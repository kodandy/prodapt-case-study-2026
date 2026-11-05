"""Real-time anomaly alerts: EWMA z-scores of per-merchant 15-minute buckets, computed on each live event."""

import math
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from contracts.events import PaymentEventV1
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ops_ai.analytics.models import AnomalyAlert
from ops_ai.clock import Clock
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope

BUCKET = timedelta(minutes=15)
LOOKBACK_BUCKETS = 24
EWMA_ALPHA = 0.3
Z_THRESHOLD = 3.0
MIN_ATTEMPTS = 10
MIN_HISTORY_BUCKETS = 4
RATE_MIN_STD = 0.02
VOLUME_MIN_STD = 1.0
DECLINED_STATE = "declined"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

AnomalyMetric = Literal["failure_rate", "volume", "decline_mix"]


@dataclass(frozen=True)
class BucketStats:
    start: datetime
    attempts: int
    declined: int
    by_code: Mapping[str, int]


@dataclass(frozen=True)
class Anomaly:
    metric: AnomalyMetric
    detail: str | None
    observed: float
    expected: float
    zscore: float


@dataclass(frozen=True)
class AnomalyAlertView:
    alert_id: uuid.UUID
    merchant_id: uuid.UUID
    metric: str
    detail: str | None
    bucket_start: datetime
    observed: float
    expected: float
    zscore: float
    attempts: int
    created_at: datetime
    updated_at: datetime


def bucket_start(at: datetime) -> datetime:
    elapsed = at.astimezone(UTC) - _EPOCH
    return _EPOCH + (elapsed // BUCKET) * BUCKET


def _ewma(history: Sequence[float], alpha: float) -> tuple[float, float]:
    mean, variance = history[0], 0.0
    for value in history[1:]:
        delta = value - mean
        mean += alpha * delta
        variance = (1 - alpha) * (variance + alpha * delta * delta)
    return mean, variance


def ewma_zscore(
    history: Sequence[float], current: float, alpha: float = EWMA_ALPHA, min_std: float = 0.0
) -> float | None:
    if len(history) < MIN_HISTORY_BUCKETS:
        return None
    mean, variance = _ewma(history, alpha)
    std = max(math.sqrt(variance), min_std)
    if std == 0.0:
        if current == mean:
            return 0.0
        return math.inf if current > mean else -math.inf
    return (current - mean) / std


def _scored(
    metric: AnomalyMetric, detail: str | None, history: Sequence[float], current: float, min_std: float
) -> Anomaly | None:
    z = ewma_zscore(history, current, min_std=min_std)
    if z is None or z < Z_THRESHOLD:
        return None
    mean, _variance = _ewma(history, EWMA_ALPHA)
    return Anomaly(metric=metric, detail=detail, observed=current, expected=mean, zscore=z)


def detect(history: Sequence[BucketStats], current: BucketStats) -> list[Anomaly]:
    """Every metric needs MIN_ATTEMPTS in the current bucket, so a few early payments never alert."""
    if current.attempts < MIN_ATTEMPTS:
        return []
    found: list[Anomaly] = []
    non_empty = [b for b in history if b.attempts > 0]
    failure = _scored(
        "failure_rate",
        None,
        [b.declined / b.attempts for b in non_empty],
        current.declined / current.attempts,
        RATE_MIN_STD,
    )
    if failure is not None:
        found.append(failure)
    volume = _scored(
        "volume", None, [float(b.attempts) for b in history], float(current.attempts), VOLUME_MIN_STD
    )
    if volume is not None:
        found.append(volume)
    mixes = [
        _scored(
            "decline_mix",
            code,
            [b.by_code.get(code, 0) / b.attempts for b in non_empty],
            count / current.attempts,
            RATE_MIN_STD,
        )
        for code, count in sorted(current.by_code.items())
    ]
    flagged = [m for m in mixes if m is not None]
    if flagged:
        found.append(max(flagged, key=lambda m: m.zscore))
    return found


def _bucket_stats(
    session: Session, merchant_id: uuid.UUID, current: datetime
) -> tuple[list[BucketStats], BucketStats]:
    first = current - LOOKBACK_BUCKETS * BUCKET
    rows = session.execute(
        select(PaymentFact.occurred_at, PaymentFact.state, PaymentFact.decline_code).where(
            PaymentFact.merchant_id == merchant_id,
            PaymentFact.occurred_at >= first,
            PaymentFact.occurred_at < current + BUCKET,
        )
    ).all()
    attempts: dict[datetime, int] = {}
    declined: dict[datetime, int] = {}
    by_code: dict[datetime, dict[str, int]] = {}
    for occurred_at, state, decline_code in rows:
        start = bucket_start(occurred_at)
        attempts[start] = attempts.get(start, 0) + 1
        if state == DECLINED_STATE:
            declined[start] = declined.get(start, 0) + 1
            if decline_code is not None:
                codes = by_code.setdefault(start, {})
                codes[decline_code] = codes.get(decline_code, 0) + 1
    stats = [
        BucketStats(
            start=start,
            attempts=attempts.get(start, 0),
            declined=declined.get(start, 0),
            by_code=by_code.get(start, {}),
        )
        for start in (first + i * BUCKET for i in range(LOOKBACK_BUCKETS + 1))
    ]
    return stats[:-1], stats[-1]


def alert_key(metric: str, merchant_id: uuid.UUID, start: datetime, detail: str | None) -> str:
    return f"{metric}:{merchant_id}:{start:%Y-%m-%dT%H:%M}:{detail or ''}"


def observe_payment(session: Session, clock: Clock, event: PaymentEventV1) -> list[uuid.UUID]:
    """Upserts one alert per anomaly in the event's bucket. Backfill events are ignored. Does not commit."""
    if event.origin != "live":
        return []
    current_start = bucket_start(event.occurred_at)
    history, current = _bucket_stats(session, event.merchant_id, current_start)
    now = clock()
    alert_ids: list[uuid.UUID] = []
    for anomaly in detect(history, current):
        statement = insert(AnomalyAlert).values(
            alert_id=uuid.uuid4(),
            alert_key=alert_key(anomaly.metric, event.merchant_id, current_start, anomaly.detail),
            merchant_id=event.merchant_id,
            metric=anomaly.metric,
            detail=anomaly.detail,
            bucket_start=current_start,
            observed=anomaly.observed,
            expected=anomaly.expected,
            zscore=anomaly.zscore,
            attempts=current.attempts,
            created_at=now,
            updated_at=now,
        )
        upsert = statement.on_conflict_do_update(
            index_elements=["alert_key"],
            set_={
                "observed": statement.excluded.observed,
                "expected": statement.excluded.expected,
                "zscore": statement.excluded.zscore,
                "attempts": statement.excluded.attempts,
                "updated_at": statement.excluded.updated_at,
            },
        ).returning(AnomalyAlert.alert_id)
        alert_ids.append(session.execute(upsert).scalar_one())
    return alert_ids


def list_alerts(
    session: Session, scope: Scope | None, since: datetime | None, limit: int
) -> list[AnomalyAlertView]:
    """`scope=None` is the ops view across every merchant; callers must have checked ops access."""
    query = select(AnomalyAlert)
    if scope is not None:
        query = query.where(AnomalyAlert.merchant_id == scope.merchant_id)
    if since is not None:
        query = query.where(AnomalyAlert.updated_at > since)
    query = query.order_by(AnomalyAlert.updated_at.desc(), AnomalyAlert.alert_id).limit(limit)
    return [
        AnomalyAlertView(
            alert_id=row.alert_id,
            merchant_id=row.merchant_id,
            metric=row.metric,
            detail=row.detail,
            bucket_start=row.bucket_start,
            observed=row.observed,
            expected=row.expected,
            zscore=row.zscore,
            attempts=row.attempts,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )
        for row in session.scalars(query)
    ]
