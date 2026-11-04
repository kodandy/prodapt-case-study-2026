import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ops_ai.clock import Clock
from ops_ai.scope import Scope
from ops_ai.tickets.models import Ticket
from ops_ai.tickets.routing import EscalationTopic, Queue, TicketSource, route_ticket

TicketStatus = Literal["open", "assigned", "resolved"]
TransitionTarget = Literal["assigned", "resolved"]
LIST_LIMIT_MAX = 100
MERCHANT_OWNED_SOURCES: frozenset[str] = frozenset({"assistant_escalation"})
"""Sources a merchant may assign and resolve; fraud, dispute and unknown-payment tickets belong to ops."""

# target → the only status it may be reached from
_ALLOWED_FROM: dict[TransitionTarget, TicketStatus] = {"assigned": "open", "resolved": "assigned"}


@dataclass(frozen=True)
class NewTicket:
    source: TicketSource
    source_key: str
    merchant_id: uuid.UUID
    title: str
    reason: str
    topic: EscalationTopic | None = None
    alert_id: uuid.UUID | None = None


@dataclass(frozen=True)
class TicketOpened:
    ticket_id: uuid.UUID
    created: bool
    """False: a ticket with this source_key already existed; ticket_id is that ticket's id."""


@dataclass(frozen=True)
class TicketView:
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


class TicketNotFoundError(Exception):
    """Missing, or owned by another merchant: the two are indistinguishable to the caller."""


class TicketNotMerchantOwnedError(Exception):
    """The merchant can read this ticket but not change it."""


class InvalidTicketTransitionError(Exception):
    pass


def open_ticket(session: Session, clock: Clock, new: NewTicket) -> TicketOpened:
    """Routes and inserts the ticket; a second call with the same source_key changes nothing.

    Does not commit.
    """
    queue, priority = route_ticket(new.source, new.topic)
    inserted = session.execute(
        insert(Ticket)
        .values(
            ticket_id=uuid.uuid4(),
            source_key=new.source_key,
            source=new.source,
            queue=queue,
            priority=priority,
            merchant_id=new.merchant_id,
            title=new.title,
            reason=new.reason,
            alert_id=new.alert_id,
            created_at=clock(),
        )
        .on_conflict_do_nothing(index_elements=["source_key"])
        .returning(Ticket.ticket_id)
    ).scalar_one_or_none()
    if inserted is not None:
        return TicketOpened(ticket_id=inserted, created=True)
    existing = session.execute(
        select(Ticket.ticket_id).where(Ticket.source_key == new.source_key)
    ).scalar_one()
    return TicketOpened(ticket_id=existing, created=False)


def list_tickets(
    session: Session,
    scope: Scope,
    *,
    status: TicketStatus | None = None,
    queue: Queue | None = None,
    limit: int = 50,
) -> list[TicketView]:
    """The scope's tickets, newest first."""
    query = select(Ticket).where(Ticket.merchant_id == scope.merchant_id)
    if status is not None:
        query = query.where(Ticket.status == status)
    if queue is not None:
        query = query.where(Ticket.queue == queue)
    query = query.order_by(Ticket.created_at.desc(), Ticket.ticket_id).limit(
        max(1, min(limit, LIST_LIMIT_MAX))
    )
    return [_view(ticket) for ticket in session.scalars(query)]


def get_ticket(session: Session, scope: Scope, ticket_id: uuid.UUID) -> TicketView | None:
    ticket = session.scalars(
        select(Ticket).where(Ticket.ticket_id == ticket_id, Ticket.merchant_id == scope.merchant_id)
    ).one_or_none()
    return None if ticket is None else _view(ticket)


def transition_ticket(
    session: Session,
    clock: Clock,
    scope: Scope,
    ticket_id: uuid.UUID,
    target: TransitionTarget,
    assignee: str | None = None,
) -> TicketView:
    """open → assigned (needs an assignee) or assigned → resolved, on merchant-owned sources only.

    Refusals in order: not found / another merchant's → TicketNotFoundError; source not in
    MERCHANT_OWNED_SOURCES → TicketNotMerchantOwnedError; else InvalidTicketTransitionError.
    One conditional UPDATE (WHERE status = the only allowed source status), so two racing transitions cannot
    both succeed. Does not commit.
    """
    values: dict[str, object] = {"status": target, "updated_at": clock()}
    if target == "assigned":
        if not assignee:
            raise _refusal(session, scope, ticket_id, target)
        values["assignee"] = assignee
    updated = session.scalars(
        update(Ticket)
        .where(
            Ticket.ticket_id == ticket_id,
            Ticket.merchant_id == scope.merchant_id,
            Ticket.source.in_(MERCHANT_OWNED_SOURCES),
            Ticket.status == _ALLOWED_FROM[target],
        )
        .values(**values)
        .returning(Ticket)
    ).one_or_none()
    if updated is not None:
        return _view(updated)
    raise _refusal(session, scope, ticket_id, target)


def _refusal(session: Session, scope: Scope, ticket_id: uuid.UUID, target: TransitionTarget) -> Exception:
    current = get_ticket(session, scope, ticket_id)
    if current is None:
        return TicketNotFoundError(str(ticket_id))
    if current.source not in MERCHANT_OWNED_SOURCES:
        return TicketNotMerchantOwnedError(str(ticket_id))
    return InvalidTicketTransitionError(f"cannot move to {target}")


def _view(ticket: Ticket) -> TicketView:
    return TicketView(
        ticket_id=ticket.ticket_id,
        source=ticket.source,
        queue=ticket.queue,
        priority=ticket.priority,
        status=ticket.status,
        title=ticket.title,
        reason=ticket.reason,
        assignee=ticket.assignee,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
    )
