import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock
from ops_ai.scope import Scope
from ops_ai.tickets.routing import Queue
from ops_ai.tickets.service import (
    LIST_LIMIT_MAX,
    InvalidTicketTransitionError,
    TicketNotFoundError,
    TicketStatus,
    TicketView,
    get_ticket,
    list_tickets,
    transition_ticket,
)

NOT_FOUND = "ticket not found"
ASSIGNEE_PATTERN = r"^[A-Za-z0-9._@-]+$"


class TicketOut(BaseModel):
    ticket_id: uuid.UUID
    source: str
    queue: str
    priority: str
    status: str
    title: str
    reason: str
    assignee: str | None
    created_at: datetime
    updated_at: datetime | None

    @classmethod
    def of(cls, view: TicketView) -> "TicketOut":
        return cls(
            ticket_id=view.ticket_id,
            source=view.source,
            queue=view.queue,
            priority=view.priority,
            status=view.status,
            title=view.title,
            reason=view.reason,
            assignee=view.assignee,
            created_at=view.created_at,
            updated_at=view.updated_at,
        )


class TicketListOut(BaseModel):
    tickets: list[TicketOut]


class TransitionRequest(BaseModel):
    """No tenant field: the merchant comes from the token only."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["assigned", "resolved"]
    assignee: Annotated[str, Field(min_length=1, max_length=64, pattern=ASSIGNEE_PATTERN)] | None = None


def create_ticket_router(
    session_factory: sessionmaker[Session], clock: Clock, merchant_scope: Callable[..., Scope]
) -> APIRouter:
    """GET /v1/tickets, GET /v1/tickets/{id}, POST /v1/tickets/{id}/transition — merchant token, own tickets
    only. Another merchant's ticket and a missing one → the identical 404."""
    router = APIRouter()

    @router.get("/v1/tickets")
    def read_tickets(
        scope: Annotated[Scope, Depends(merchant_scope)],
        response: Response,
        ticket_status: Annotated[TicketStatus | None, Query(alias="status")] = None,
        queue: Queue | None = None,
        limit: Annotated[int, Query(ge=1, le=LIST_LIMIT_MAX)] = 50,
    ) -> TicketListOut:
        with session_factory() as session:
            views = list_tickets(session, scope, status=ticket_status, queue=queue, limit=limit)
        response.headers["Cache-Control"] = "no-store"
        return TicketListOut(tickets=[TicketOut.of(view) for view in views])

    @router.get("/v1/tickets/{ticket_id}")
    def read_ticket(
        ticket_id: uuid.UUID, scope: Annotated[Scope, Depends(merchant_scope)], response: Response
    ) -> TicketOut:
        with session_factory() as session:
            view = get_ticket(session, scope, ticket_id)
        if view is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND, headers={"Cache-Control": "no-store"})
        response.headers["Cache-Control"] = "no-store"
        return TicketOut.of(view)

    @router.post("/v1/tickets/{ticket_id}/transition")
    def move_ticket(
        ticket_id: uuid.UUID,
        body: TransitionRequest,
        scope: Annotated[Scope, Depends(merchant_scope)],
        response: Response,
    ) -> TicketOut:
        try:
            with session_factory.begin() as session:
                view = transition_ticket(session, clock, scope, ticket_id, body.status, body.assignee)
        except TicketNotFoundError:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, NOT_FOUND, headers={"Cache-Control": "no-store"}
            ) from None
        except InvalidTicketTransitionError:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "invalid ticket transition", headers={"Cache-Control": "no-store"}
            ) from None
        response.headers["Cache-Control"] = "no-store"
        return TicketOut.of(view)

    return router
