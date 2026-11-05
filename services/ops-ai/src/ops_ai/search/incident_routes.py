from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.scope import Scope
from ops_ai.search.embedder import Embedder
from ops_ai.search.rerank import RankedIncident, search_incidents_reranked


class IncidentSearchRequest(BaseModel):
    """No tenant field: the merchant comes from the token only."""

    model_config = ConfigDict(extra="forbid")

    query: Annotated[StrictStr, Field(min_length=1, max_length=500)]
    limit: Annotated[StrictInt, Field(ge=1, le=20)] = 5


class IncidentResultOut(BaseModel):
    incident_id: str
    title: str
    root_cause: str
    fix_applied: str
    resolved_at: datetime | None
    recurrence_within_7d: bool
    relevance: float
    recency: float
    effectiveness: float
    score: float

    @classmethod
    def of(cls, ranked: RankedIncident) -> "IncidentResultOut":
        return cls(
            incident_id=ranked.incident_id,
            title=ranked.title,
            root_cause=ranked.root_cause,
            fix_applied=ranked.fix_applied,
            resolved_at=ranked.resolved_at,
            recurrence_within_7d=ranked.recurrence_within_7d,
            relevance=ranked.relevance,
            recency=ranked.recency,
            effectiveness=ranked.effectiveness,
            score=ranked.score,
        )


class IncidentSearchResponse(BaseModel):
    results: list[IncidentResultOut]


def create_incident_search_router(
    session_factory: sessionmaker[Session],
    clock: Clock,
    embedder: Embedder,
    merchant_scope: Callable[..., Scope],
) -> APIRouter:
    """POST /v1/incidents/search (merchant token): hybrid search over visible incidents, reranked (R2-4)."""
    router = APIRouter()

    @router.post("/v1/incidents/search")
    def post_incident_search(
        body: IncidentSearchRequest, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> IncidentSearchResponse:
        with session_factory() as session:
            ranked = search_incidents_reranked(
                session, scope, body.query, now=clock(), embedder=embedder, limit=body.limit
            )
        response.headers["Cache-Control"] = "no-store"
        return IncidentSearchResponse(results=[IncidentResultOut.of(item) for item in ranked])

    return router
