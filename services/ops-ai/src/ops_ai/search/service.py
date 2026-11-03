import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.scope import Scope
from ops_ai.search.models import SearchDocument


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
