"""Keyset-paginated dispute lists (newest first) for the merchant and for the paying user."""

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session

from gateway.payments.disputes import dispute_view
from gateway.payments.history import decode_cursor, encode_cursor
from gateway.payments.models import Dispute, Payment
from gateway.payments.schemas import DisputeState, DisputeView


@dataclass(frozen=True)
class DisputePage:
    items: list[DisputeView]
    next_cursor: str | None


def list_merchant_disputes(
    session: Session,
    merchant_id: uuid.UUID,
    limit: int,
    cursor: str | None = None,
    state: DisputeState | None = None,
) -> DisputePage:
    """Raises InvalidCursorError for a malformed cursor."""
    query = select(Dispute).where(Dispute.merchant_id == merchant_id)
    if state is not None:
        query = query.where(Dispute.state == state)
    return _page(session, query, limit, cursor)


def list_user_disputes(
    session: Session, user_id: uuid.UUID, limit: int, cursor: str | None = None
) -> DisputePage:
    """Disputes on payments the user paid, whoever opened them."""
    query = (
        select(Dispute)
        .join(Payment, Payment.id == Dispute.payment_id)
        .where(Payment.payer_user_id == user_id)
    )
    return _page(session, query, limit, cursor)


def _page(session: Session, query: Select[Dispute], limit: int, cursor: str | None) -> DisputePage:
    if cursor is not None:
        created_at, dispute_id = decode_cursor(cursor)
        query = query.where(tuple_(Dispute.created_at, Dispute.id) < tuple_(created_at, dispute_id))
    rows = list(
        session.scalars(query.order_by(Dispute.created_at.desc(), Dispute.id.desc()).limit(limit + 1))
    )
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return DisputePage(items=[dispute_view(d) for d in page], next_cursor=next_cursor)
