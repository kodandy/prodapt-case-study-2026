"""Historical incident reranking (R2-4): hybrid search first, then recency and resolution effectiveness.

Effectiveness blends whether the incident was resolved, whether it came back within 7 days and the merchant's
own helpful / not-helpful feedback (Laplace-smoothed), so one vote cannot dominate.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from ops_ai.feedback.service import incident_scores
from ops_ai.investigation.models import IncidentRecord
from ops_ai.scope import Scope
from ops_ai.search.embedder import Embedder, EmbedderUnavailableError, cosine_similarity, is_zero
from ops_ai.search.service import DEFAULT_MIN_SIMILARITY, SearchHit, reciprocal_rank_fusion

RECENCY_HALF_LIFE_DAYS = 30.0
WEIGHT_RELEVANCE, WEIGHT_RECENCY, WEIGHT_EFFECTIVENESS = 0.5, 0.2, 0.3
REOPEN_PENALTY = 0.5
KEYWORD_CANDIDATES = 30
VECTOR_CANDIDATES = 200
_INCIDENT_NAMESPACE = uuid.UUID("6f1c2d3e-4b5a-4c7d-8e9f-a0b1c2d3e4f5")
_SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class IncidentCandidate:
    incident_id: str
    title: str
    root_cause: str
    fix_applied: str
    started_at: datetime
    resolved_at: datetime | None
    recurrence_within_7d: bool
    relevance: float
    helpful: int = 0
    not_helpful: int = 0


@dataclass(frozen=True)
class RankedIncident:
    incident_id: str
    title: str
    root_cause: str
    fix_applied: str
    started_at: datetime
    resolved_at: datetime | None
    recurrence_within_7d: bool
    relevance: float
    recency: float
    effectiveness: float
    score: float


def effectiveness(resolved: bool, recurrence_within_7d: bool, helpful: int, not_helpful: int) -> float:
    if not resolved:
        return 0.0
    value = (helpful + 1) / (helpful + not_helpful + 2)
    return value * REOPEN_PENALTY if recurrence_within_7d else value


def recency(at: datetime, now: datetime) -> float:
    age_days = (now - at).total_seconds() / _SECONDS_PER_DAY
    return float(0.5 ** (max(age_days, 0.0) / RECENCY_HALF_LIFE_DAYS))


def rerank_incidents(candidates: Sequence[IncidentCandidate], now: datetime) -> list[RankedIncident]:
    top = max((candidate.relevance for candidate in candidates), default=0.0)
    ranked: list[RankedIncident] = []
    for candidate in candidates:
        relevance = candidate.relevance / top if top > 0 else 0.0
        fresh = recency(candidate.resolved_at or candidate.started_at, now)
        effective = effectiveness(
            candidate.resolved_at is not None,
            candidate.recurrence_within_7d,
            candidate.helpful,
            candidate.not_helpful,
        )
        ranked.append(
            RankedIncident(
                incident_id=candidate.incident_id,
                title=candidate.title,
                root_cause=candidate.root_cause,
                fix_applied=candidate.fix_applied,
                started_at=candidate.started_at,
                resolved_at=candidate.resolved_at,
                recurrence_within_7d=candidate.recurrence_within_7d,
                relevance=relevance,
                recency=fresh,
                effectiveness=effective,
                score=WEIGHT_RELEVANCE * relevance
                + WEIGHT_RECENCY * fresh
                + WEIGHT_EFFECTIVENESS * effective,
            )
        )
    ranked.sort(key=lambda item: (-item.score, item.incident_id))
    return ranked


def _visible(scope: Scope) -> ColumnElement[bool]:
    return or_(IncidentRecord.merchant_id == scope.merchant_id, IncidentRecord.merchant_id.is_(None))


def _incident_text(incident: IncidentRecord) -> str:
    return f"{incident.title} {incident.root_cause} {incident.fix_applied}"


def _hit(incident: IncidentRecord, rank: float) -> SearchHit:
    return SearchHit(
        kind="incident",
        ref_id=uuid.uuid5(_INCIDENT_NAMESPACE, incident.incident_id),
        title=incident.title,
        body=incident.root_cause,
        occurred_at=incident.started_at,
        rank=rank,
    )


def _keyword_leg(session: Session, scope: Scope, query: str) -> list[tuple[IncidentRecord, float]]:
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(IncidentRecord.tsv, tsquery).label("rank")
    rows = session.execute(
        select(IncidentRecord, rank)
        .where(_visible(scope), IncidentRecord.tsv.op("@@")(tsquery))
        .order_by(rank.desc(), IncidentRecord.started_at.desc(), IncidentRecord.incident_id)
        .limit(KEYWORD_CANDIDATES)
    ).all()
    return [(row[0], float(row[1])) for row in rows]


def _vector_leg(
    session: Session, scope: Scope, query: str, embedder: Embedder
) -> list[tuple[IncidentRecord, float]]:
    incidents = list(
        session.execute(
            select(IncidentRecord)
            .where(_visible(scope))
            .order_by(IncidentRecord.started_at.desc(), IncidentRecord.incident_id)
            .limit(VECTOR_CANDIDATES)
        ).scalars()
    )
    if not incidents:
        return []
    try:
        vectors = embedder.embed([query, *(_incident_text(incident) for incident in incidents)])
    except EmbedderUnavailableError:
        return []
    query_vector, incident_vectors = vectors[0], vectors[1:]
    if is_zero(query_vector):
        return []
    scored = [
        (incident, cosine_similarity(query_vector, vector))
        for incident, vector in zip(incidents, incident_vectors, strict=True)
        if not is_zero(vector)
    ]
    kept = [(incident, similarity) for incident, similarity in scored if similarity >= DEFAULT_MIN_SIMILARITY]
    kept.sort(key=lambda pair: (-pair[1], pair[0].incident_id))
    return kept


def search_incidents_reranked(
    session: Session, scope: Scope, query: str, now: datetime, embedder: Embedder, limit: int = 5
) -> list[RankedIncident]:
    """Hybrid (FTS + embedder, RRF) over the incidents the merchant can see, then rerank_incidents,
    top `limit`."""
    keyword = _keyword_leg(session, scope, query)
    vector = _vector_leg(session, scope, query, embedder)
    by_ref: dict[uuid.UUID, IncidentRecord] = {}
    rankings: list[list[SearchHit]] = []
    for leg in (keyword, vector):
        hits = []
        for incident, rank in leg:
            hit = _hit(incident, rank)
            by_ref[hit.ref_id] = incident
            hits.append(hit)
        rankings.append(hits)
    fused = reciprocal_rank_fusion(rankings, limit=max(len(by_ref), 1))
    if not fused:
        return []
    scores = incident_scores(session, scope, [by_ref[hit.ref_id].incident_id for hit in fused])
    candidates: list[IncidentCandidate] = []
    for hit in fused:
        incident = by_ref[hit.ref_id]
        score = scores.get(incident.incident_id)
        candidates.append(
            IncidentCandidate(
                incident_id=incident.incident_id,
                title=incident.title,
                root_cause=incident.root_cause,
                fix_applied=incident.fix_applied,
                started_at=incident.started_at,
                resolved_at=incident.resolved_at,
                recurrence_within_7d=incident.recurrence_within_7d,
                relevance=hit.rank,
                helpful=0 if score is None else score.helpful,
                not_helpful=0 if score is None else score.not_helpful,
            )
        )
    return rerank_incidents(candidates, now)[:limit]
