"""B11.1 / B11.2: reading notification records, marking them read, answering "was this you?" notices."""

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, JsonValue
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.notifications.models import Notification
from gateway.notifications.records import RecipientType
from gateway.payments.disputes import (
    DisputeAlreadyOpenError,
    DisputeAmountExceedsError,
    PaymentNotDisputableError,
    PaymentNotFoundError,
    open_dispute_in_session,
)
from gateway.payments.history import decode_cursor, encode_cursor

NOT_ME_DISPUTE_REASON = "fraudulent_not_me"
Answer = Literal["confirmed", "not_me"]


class NotificationNotFoundError(Exception):
    """Unknown, or addressed to another recipient."""


class NotAWasThisYouError(Exception):
    pass


class AlreadyAnsweredError(Exception):
    pass


class NotificationView(BaseModel):
    notification_id: uuid.UUID
    kind: str
    payment_id: uuid.UUID
    payload: dict[str, JsonValue]
    created_at: datetime
    read: bool
    response: str | None


@dataclass(frozen=True)
class NotificationPage:
    items: list[NotificationView]
    next_cursor: str | None


@dataclass(frozen=True)
class AnswerResult:
    notification_id: uuid.UUID
    response: Answer
    dispute_id: uuid.UUID | None


def list_notifications(
    session: Session,
    recipient_type: RecipientType,
    recipient_id: uuid.UUID,
    limit: int,
    cursor: str | None = None,
    unread_only: bool = False,
) -> NotificationPage:
    """Newest first. Raises InvalidCursorError for a malformed cursor."""
    query = select(Notification).where(
        Notification.recipient_type == recipient_type, Notification.recipient_id == recipient_id
    )
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    if cursor is not None:
        created_at, notification_id = decode_cursor(cursor)
        query = query.where(
            tuple_(Notification.created_at, Notification.id) < tuple_(created_at, notification_id)
        )
    order = (Notification.created_at.desc(), Notification.id.desc())
    rows = list(session.scalars(query.order_by(*order).limit(limit + 1)))
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit else None
    return NotificationPage(items=[notification_view(n) for n in page], next_cursor=next_cursor)


def mark_read(
    session_factory: sessionmaker[Session],
    clock: Clock,
    recipient_type: RecipientType,
    recipient_id: uuid.UUID,
    notification_id: uuid.UUID,
) -> NotificationView:
    """Idempotent: the first read time is kept. Raises NotificationNotFoundError."""
    with session_factory.begin() as session:
        notification = _lock_own(session, recipient_type, recipient_id, notification_id)
        if notification.read_at is None:
            notification.read_at = clock()
        return notification_view(notification)


def answer_was_this_you(
    session_factory: sessionmaker[Session],
    clock: Clock,
    user_id: uuid.UUID,
    notification_id: uuid.UUID,
    answer: Answer,
) -> AnswerResult:
    """Records the user's answer once. `not_me` opens a dispute for the still-refundable amount, as the
    user, in the same transaction, when the payment succeeded and has no active dispute.

    Raises NotificationNotFoundError, NotAWasThisYouError, AlreadyAnsweredError.
    """
    with session_factory.begin() as session:
        notification = _lock_own(session, "user", user_id, notification_id)
        if notification.kind != "was_this_you":
            raise NotAWasThisYouError
        if notification.response is not None:
            raise AlreadyAnsweredError
        now = clock()
        notification.response = answer
        notification.responded_at = now
        if notification.read_at is None:
            notification.read_at = now
        dispute_id = None
        if answer == "not_me":
            dispute_id = _dispute_as_payer(session, clock, user_id, notification.payment_id)
        return AnswerResult(notification_id=notification.id, response=answer, dispute_id=dispute_id)


def _dispute_as_payer(
    session: Session, clock: Clock, user_id: uuid.UUID, payment_id: uuid.UUID
) -> uuid.UUID | None:
    try:
        dispute = open_dispute_in_session(
            session,
            clock,
            payment_id,
            NOT_ME_DISPUTE_REASON,
            payer_user_id=user_id,
            actor_id=f"user:{user_id}",
            actor_role="consumer",
        )
    except (
        PaymentNotFoundError,
        PaymentNotDisputableError,
        DisputeAlreadyOpenError,
        DisputeAmountExceedsError,
    ):
        return None
    return dispute.dispute_id


def _lock_own(
    session: Session, recipient_type: RecipientType, recipient_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification:
    notification = session.scalar(
        select(Notification)
        .where(
            Notification.id == notification_id,
            Notification.recipient_type == recipient_type,
            Notification.recipient_id == recipient_id,
        )
        .with_for_update()
    )
    if notification is None:
        raise NotificationNotFoundError
    return notification


def notification_view(notification: Notification) -> NotificationView:
    return NotificationView(
        notification_id=notification.id,
        kind=notification.kind,
        payment_id=notification.payment_id,
        payload=notification.payload,
        created_at=notification.created_at,
        read=notification.read_at is not None,
        response=notification.response,
    )
