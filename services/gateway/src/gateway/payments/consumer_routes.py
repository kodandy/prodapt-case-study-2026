"""B13.1 consumer payment routes: pay a merchant, payment history, ask the merchant for a refund."""

import re
import unicodedata
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from gateway.clock import Clock
from gateway.compliance.kyc import ensure_not_rejected
from gateway.payments.charge import ChargeDeps
from gateway.payments.consumer import (
    ConsumerPayment,
    RefundRequestError,
    list_user_payments,
    pay_merchant,
    request_refund,
)
from gateway.payments.history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, InvalidCursorError
from gateway.payments.schemas import MAX_AMOUNT_MINOR, ChargeRequest
from gateway.payments.state import PaymentState

NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
USER_IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,200}")
"""Shorter than the merchant limit: the stored key is prefixed with `user:<user id>:`."""
MAX_NOTE_LENGTH = 280
REFUND_ERROR_STATUS = {
    "payment_not_found": status.HTTP_404_NOT_FOUND,
    "payment_not_refundable": status.HTTP_409_CONFLICT,
    "refund_exceeds_refundable": status.HTTP_409_CONFLICT,
    "refund_already_requested": status.HTTP_409_CONFLICT,
}


class ConsumerPayRequest(ChargeRequest):
    merchant_id: uuid.UUID


class RefundRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: StrictInt | None = Field(default=None, gt=0, le=MAX_AMOUNT_MINOR)
    """Omitted → the whole refundable amount."""
    note: str | None = Field(default=None, max_length=MAX_NOTE_LENGTH)

    @field_validator("note")
    @classmethod
    def _no_control_or_format(cls, value: str | None) -> str | None:
        if value is not None and any(unicodedata.category(char) in ("Cc", "Cf") for char in value):
            raise ValueError("note must not contain control or format characters")
        return value


class RefundRequestResponse(BaseModel):
    payment_id: uuid.UUID
    requested_minor: int
    state: str = "requested"


class ConsumerPaymentListResponse(BaseModel):
    items: list[ConsumerPayment]
    next_cursor: str | None


def consumer_payment_router(
    charge: ChargeDeps, clock: Clock, user_auth: Callable[[str], Callable[..., uuid.UUID]]
) -> APIRouter:
    """Routes (Bearer session; every response carries Cache-Control: no-store):
    - POST /v1/me/payments (Idempotency-Key, visible ASCII, 1-200 chars) {merchant_id, amount_minor, currency,
      card, settlement_currency?} → the charge outcome; unknown merchant → 404 merchant_not_found; bad or
      missing key → 400.
    - GET /v1/me/payments?limit&cursor&state&created_from&created_to → {items, next_cursor}: payments the user
      paid, newest first.
    - POST /v1/me/payments/{payment_id}/refund-requests {amount_minor?, note?} → 201 {payment_id,
      requested_minor, state: "requested"}: notifies the merchant, moves no money; one per payment.
    """
    router = APIRouter()
    session_factory = charge.session_factory

    @router.post("/v1/me/payments")
    def pay(
        body: ConsumerPayRequest,
        user_id: Annotated[uuid.UUID, Depends(user_auth("me.transfer.create"))],
        idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "Idempotency-Key header is required", headers=NO_STORE_HEADERS
            )
        if USER_IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "invalid Idempotency-Key header", headers=NO_STORE_HEADERS
            )
        if not ensure_not_rejected(session_factory, clock, user_id, "payment"):
            return JSONResponse(
                {"error": "kyc_reverification_required"},
                status_code=status.HTTP_403_FORBIDDEN,
                headers=NO_STORE_HEADERS,
            )
        request = ChargeRequest(
            amount_minor=body.amount_minor,
            currency=body.currency,
            method=body.method,
            card=body.card,
            settlement_currency=body.settlement_currency,
        )
        outcome = pay_merchant(charge, user_id, body.merchant_id, idempotency_key, request)
        return JSONResponse(outcome.body, status_code=outcome.status_code, headers=NO_STORE_HEADERS)

    @router.get("/v1/me/payments")
    def my_payments(
        user_id: Annotated[uuid.UUID, Depends(user_auth("me.transfer.read"))],
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        state: PaymentState | None = None,
        created_from: Annotated[str | None, Query(max_length=64)] = None,
        created_to: Annotated[str | None, Query(max_length=64)] = None,
    ) -> JSONResponse:
        start = _aware_datetime(created_from, "created_from")
        end = _aware_datetime(created_to, "created_to")
        if start is not None and end is not None and start >= end:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid date range", headers=NO_STORE_HEADERS)
        try:
            with session_factory() as session:
                page = list_user_payments(session, user_id, limit, cursor, state, start, end)
        except InvalidCursorError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "invalid cursor", headers=NO_STORE_HEADERS
            ) from None
        response = ConsumerPaymentListResponse(items=page.items, next_cursor=page.next_cursor)
        return JSONResponse(response.model_dump(mode="json"), headers=NO_STORE_HEADERS)

    @router.post("/v1/me/payments/{payment_id}/refund-requests")
    def ask_for_refund(
        payment_id: uuid.UUID,
        body: RefundRequestBody,
        user_id: Annotated[uuid.UUID, Depends(user_auth("me.transfer.create"))],
    ) -> JSONResponse:
        try:
            result = request_refund(session_factory, clock, user_id, payment_id, body.amount_minor, body.note)
        except RefundRequestError as error:
            return JSONResponse(
                {"error": error.code}, status_code=REFUND_ERROR_STATUS[error.code], headers=NO_STORE_HEADERS
            )
        response = RefundRequestResponse(payment_id=result.payment_id, requested_minor=result.requested_minor)
        return JSONResponse(response.model_dump(mode="json"), status_code=201, headers=NO_STORE_HEADERS)

    return router


def _aware_datetime(value: str | None, name: str) -> datetime | None:
    """ISO 8601 with an offset, in UTC; naive, date-only or out-of-range → 400."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"invalid {name}", headers=NO_STORE_HEADERS
        ) from None
