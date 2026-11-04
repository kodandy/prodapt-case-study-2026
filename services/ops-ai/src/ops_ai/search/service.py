import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.scope import Scope
from ops_ai.search.embedder import Embedder, EmbedderUnavailableError, is_zero
from ops_ai.search.models import EMBEDDING_DIMENSIONS, SearchDocument, embedding_column
from ops_ai.search.vector_type import cosine_distance

RRF_K = 60
DEFAULT_MIN_SIMILARITY = 0.4
MIN_LEG_CANDIDATES = 30


@dataclass(frozen=True)
class SearchHit:
    kind: str
    ref_id: uuid.UUID
    title: str
    body: str
    occurred_at: datetime
    rank: float


def search_documents(session: Session, scope: Scope, query: str, limit: int = 10) -> list[SearchHit]:
    """Full-text search (websearch_to_tsquery, english) restricted to scope.merchant_id, best rank first."""
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank(SearchDocument.tsv, tsquery).label("rank")
    rows = session.execute(
        select(
            SearchDocument.kind,
            SearchDocument.ref_id,
            SearchDocument.title,
            SearchDocument.body,
            SearchDocument.occurred_at,
            rank,
        )
        .where(SearchDocument.merchant_id == scope.merchant_id, SearchDocument.tsv.op("@@")(tsquery))
        .order_by(rank.desc(), SearchDocument.occurred_at.desc())
        .limit(limit)
    ).all()
    return [
        SearchHit(
            kind=row.kind,
            ref_id=row.ref_id,
            title=row.title,
            body=row.body,
            occurred_at=row.occurred_at,
            rank=float(row.rank),
        )
        for row in rows
    ]


def vector_search(
    session: Session,
    scope: Scope,
    query: str,
    embedder: Embedder,
    limit: int = 10,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> list[SearchHit]:
    """Cosine similarity over the embedder's own column, restricted to scope.merchant_id and to rows that
    have a vector for that embedder; hits below `min_similarity` are dropped. rank = cosine similarity.

    Raises ValueError for an embedder without a column; EmbedderUnavailableError propagates.
    """
    column = embedding_column(embedder.embedder_id)
    dimensions = EMBEDDING_DIMENSIONS[embedder.embedder_id]
    if embedder.dimensions != dimensions:
        raise ValueError(f"embedder {embedder.embedder_id!r} must produce {dimensions} dimensions")
    [query_vector] = embedder.embed([query])
    if is_zero(query_vector):
        return []
    distance = cosine_distance(column, query_vector, dimensions)
    similarity = (1.0 - distance).label("rank")
    rows = session.execute(
        select(
            SearchDocument.kind,
            SearchDocument.ref_id,
            SearchDocument.title,
            SearchDocument.body,
            SearchDocument.occurred_at,
            similarity,
        )
        .where(
            SearchDocument.merchant_id == scope.merchant_id,
            column.is_not(None),
            distance <= 1.0 - min_similarity,
        )
        .order_by(distance, SearchDocument.occurred_at.desc(), SearchDocument.kind, SearchDocument.ref_id)
        .limit(limit)
    ).all()
    return [
        SearchHit(
            kind=row.kind,
            ref_id=row.ref_id,
            title=row.title,
            body=row.body,
            occurred_at=row.occurred_at,
            rank=float(row.rank),
        )
        for row in rows
    ]


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[SearchHit]], k: int = RRF_K, limit: int = 10
) -> list[SearchHit]:
    """score(doc) = sum over rankings containing it of 1 / (k + position), position 1-based.
    Identity is (kind, ref_id); fields come from the first ranking holding the doc; rank = score.
    """
    scores: dict[tuple[str, uuid.UUID], float] = {}
    first_seen: dict[tuple[str, uuid.UUID], SearchHit] = {}
    for ranking in rankings:
        seen_here: set[tuple[str, uuid.UUID]] = set()
        for position, hit in enumerate(ranking, start=1):
            key = (hit.kind, hit.ref_id)
            if key in seen_here:
                continue
            seen_here.add(key)
            first_seen.setdefault(key, hit)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + position)
    fused = [replace(first_seen[key], rank=score) for key, score in scores.items()]
    fused.sort(key=lambda hit: (hit.kind, str(hit.ref_id)))
    fused.sort(key=lambda hit: hit.occurred_at, reverse=True)
    fused.sort(key=lambda hit: hit.rank, reverse=True)
    return fused[:limit]


def hybrid_search(
    session: Session,
    scope: Scope,
    query: str,
    embedder: Embedder,
    limit: int = 10,
    k: int = RRF_K,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> list[SearchHit]:
    """Keyword (FTS) + vector legs, both scoped to scope.merchant_id, fused with RRF.
    An unavailable embedder degrades to the keyword leg alone.
    """
    candidates = max(limit * 3, MIN_LEG_CANDIDATES)
    keyword = search_documents(session, scope, query, limit=candidates)
    try:
        vector = vector_search(
            session, scope, query, embedder, limit=candidates, min_similarity=min_similarity
        )
    except EmbedderUnavailableError:
        vector = []
    return reciprocal_rank_fusion([keyword, vector], k=k, limit=limit)
