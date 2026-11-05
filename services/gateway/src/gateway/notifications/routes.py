"""B11.1 / B11.2 notification routes for users (session) and merchants (API key)."""

import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.notifications.inbox import (
    AlreadyAnsweredError,
    Answer,
    NotAWasThisYouError,
    NotificationNotFoundError,
    NotificationView,
    answer_was_this_you,
    list_notifications,
    mark_read,
)
from gateway.notifications.records import RecipientType
from gateway.payments.history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, InvalidCursorError


class NotificationListResponse(BaseModel):
    items: list[NotificationView]
    next_cursor: str | None


class AnswerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: Answer


class AnswerResponse(BaseModel):
    notification_id: uuid.UUID
    response: Answer
    dispute_id: uuid.UUID | None


def notification_router(
    session_factory: sessionmaker[Session],
    clock: Clock,
    merchant_auth: Callable[..., uuid.UUID],
    user_auth: Callable[..., uuid.UUID],
) -> APIRouter:
    """Routes:
    - GET /v1/me/notifications?limit&cursor&unread_only (Bearer session), GET /v1/notifications?... (Bearer
      API key) → {items, next_cursor}: the caller's own records, newest first; bad cursor → 400.
    - POST /v1/me/notifications/{id}/read, POST /v1/notifications/{id}/read → NotificationView; idempotent;
      unknown or someone else's → 404 "notification not found".
    - POST /v1/me/notifications/{id}/response {answer: confirmed | not_me} → {notification_id, response,
      dispute_id}; `not_me` may open a dispute on the payment as the user. Not a was_this_you → 409; already
      answered → 409.
    """
    router = APIRouter()

    def listing(
        recipient_type: RecipientType,
        recipient_id: uuid.UUID,
        limit: int,
        cursor: str | None,
        unread_only: bool,
    ) -> NotificationListResponse:
        try:
            with session_factory() as session:
                page = list_notifications(session, recipient_type, recipient_id, limit, cursor, unread_only)
        except InvalidCursorError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
        return NotificationListResponse(items=page.items, next_cursor=page.next_cursor)

    def reading(
        recipient_type: RecipientType, recipient_id: uuid.UUID, notification_id: uuid.UUID
    ) -> NotificationView:
        try:
            return mark_read(session_factory, clock, recipient_type, recipient_id, notification_id)
        except NotificationNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found") from None

    @router.get("/v1/me/notifications")
    def my_notifications(
        user_id: Annotated[uuid.UUID, Depends(user_auth)],
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        unread_only: bool = False,
    ) -> NotificationListResponse:
        return listing("user", user_id, limit, cursor, unread_only)

    @router.get("/v1/notifications")
    def merchant_notifications(
        merchant_id: Annotated[uuid.UUID, Depends(merchant_auth)],
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        unread_only: bool = False,
    ) -> NotificationListResponse:
        return listing("merchant", merchant_id, limit, cursor, unread_only)

    @router.post("/v1/me/notifications/{notification_id}/read")
    def read_my_notification(
        notification_id: uuid.UUID, user_id: Annotated[uuid.UUID, Depends(user_auth)]
    ) -> NotificationView:
        return reading("user", user_id, notification_id)

    @router.post("/v1/notifications/{notification_id}/read")
    def read_merchant_notification(
        notification_id: uuid.UUID, merchant_id: Annotated[uuid.UUID, Depends(merchant_auth)]
    ) -> NotificationView:
        return reading("merchant", merchant_id, notification_id)

    @router.post("/v1/me/notifications/{notification_id}/response")
    def answer_notification(
        notification_id: uuid.UUID, body: AnswerRequest, user_id: Annotated[uuid.UUID, Depends(user_auth)]
    ) -> AnswerResponse:
        try:
            result = answer_was_this_you(session_factory, clock, user_id, notification_id, body.answer)
        except NotificationNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "notification not found") from None
        except NotAWasThisYouError:
            raise HTTPException(status.HTTP_409_CONFLICT, "not a was-this-you notice") from None
        except AlreadyAnsweredError:
            raise HTTPException(status.HTTP_409_CONFLICT, "already answered") from None
        return AnswerResponse(
            notification_id=result.notification_id, response=result.response, dispute_id=result.dispute_id
        )

    return router
