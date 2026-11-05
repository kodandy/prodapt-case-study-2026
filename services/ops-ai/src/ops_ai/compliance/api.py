import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.compliance.models import SubjectErasure
from ops_ai.scope import OpsScope

ERASURES_LIMIT_MAX = 500


class ErasureOut(BaseModel):
    request_id: uuid.UUID
    processed_at: datetime
    rows_pseudonymised: int


class ErasureListOut(BaseModel):
    items: list[ErasureOut]


def create_compliance_router(
    session_factory: sessionmaker[Session], ops_scope: Callable[..., OpsScope]
) -> APIRouter:
    """GET /v1/ops/compliance/erasures (ops token, any ops role) → {items}: processed erasure requests, newest
    first. Non-ops tokens → 403 via `ops_scope`."""
    router = APIRouter()

    @router.get("/v1/ops/compliance/erasures")
    def list_erasures(
        _scope: Annotated[OpsScope, Depends(ops_scope)],
        response: Response,
        limit: Annotated[int, Query(ge=1, le=ERASURES_LIMIT_MAX)] = 100,
    ) -> ErasureListOut:
        response.headers["Cache-Control"] = "no-store"
        with session_factory() as session:
            rows = session.scalars(
                select(SubjectErasure)
                .order_by(SubjectErasure.processed_at.desc(), SubjectErasure.request_id)
                .limit(limit)
            ).all()
        return ErasureListOut(
            items=[
                ErasureOut(
                    request_id=row.request_id,
                    processed_at=row.processed_at,
                    rows_pseudonymised=row.rows_pseudonymised,
                )
                for row in rows
            ]
        )

    return router
