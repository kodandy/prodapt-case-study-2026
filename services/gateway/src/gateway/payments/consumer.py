"""Consumer side of payments (B13.1): pay a merchant, payment history, ask the merchant for a refund."""

import uuid
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import Merchant
from gateway.clock import Clock
from gateway.notifications.records import add_notification
from gateway.payments.charge import ChargeDeps, ChargeOutcome, charge_card
from gateway.payments.history import decode_cursor, encode_cursor
from gateway.payments.models import Payment
from gateway.payments.refunds import lock_payment, refundable_minor
from gateway.payments.schemas import CardSummary, ChargeRequest
from gateway.payments.state import PaymentState
from gateway.payments.tokenization.models import CardToken

MERCHANT_NOT_FOUND = ChargeOutcome(404, {"error": "merchant_not_found"})


class ConsumerPayment(BaseModel):
    """A payment as its payer sees it. `card` carries BIN + last4 only."""

    payment_id: uuid.UUID
    merchant_id: uuid.UUID
    state: PaymentState
    amount_minor: int
    currency: str
    card: CardSummary | None
    decline_code: str | None
    refunded_minor: int
    disputed_minor: int
    chargeback_minor: int
    chargeback_flag: bool
    created_at: datetime


class RefundRequestError(Exception):
    """`code` is the API error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ConsumerPaymentPage:
    items: list[ConsumerPayment]
    next_cursor: str | None


@dataclass(frozen=True)
class RefundRequestResult:
    payment_id: uuid.UUID
    requested_minor: int


USER_KEY_PREFIX = "user:"


def user_idempotency_key(user_id: uuid.UUID, key: str) -> str:
    """The merchant-scoped idempotency key of a user's payment.

    `POST /v1/payments` rejects merchant keys with this prefix, so the two never collide.
    """
    return f"{USER_KEY_PREFIX}{user_id}:{key}"


def pay_merchant(
    deps: ChargeDeps, user_id: uuid.UUID, merchant_id: uuid.UUID, idempotency_key: str, request: ChargeRequest
) -> ChargeOutcome:
    with deps.session_factory() as session:
        if session.get(Merchant, merchant_id) is None:
            return MERCHANT_NOT_FOUND
    return charge_card(deps, merchant_id, user_idempotency_key(user_id, idempotency_key), request, user_id)


def list_user_payments(
    session: Session,
    user_id: uuid.UUID,
    limit: int,
    cursor: str | None = None,
    state: PaymentState | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> ConsumerPaymentPage:
    """Payments the user paid, newest first; half-open date range on aware datetimes. Raises
    InvalidCursorError for a malformed cursor."""
    query = (
        select(Payment, CardToken.bin, CardToken.last4)
        .outerjoin(CardToken, CardToken.token == Payment.card_token)
        .where(Payment.payer_user_id == user_id)
    )
    if state is not None:
        query = query.where(Payment.state == state)
    if created_from is not None:
        query = query.where(Payment.created_at >= created_from)
    if created_to is not None:
        query = query.where(Payment.created_at < created_to)
    if cursor is not None:
        created_at, payment_id = decode_cursor(cursor)
        query = query.where(tuple_(Payment.created_at, Payment.id) < tuple_(created_at, payment_id))
    rows = session.execute(
        query.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return ConsumerPaymentPage(
        items=[_consumer_payment(payment, card_bin, last4) for payment, card_bin, last4 in page],
        next_cursor=next_cursor,
    )


def request_refund(
    session_factory: sessionmaker[Session],
    clock: Clock,
    user_id: uuid.UUID,
    payment_id: uuid.UUID,
    amount_minor: int | None,
    note: str | None,
) -> RefundRequestResult:
    """Notifies the merchant (`refund_requested`); moves no money. One request per payment.

    Raises RefundRequestError: payment_not_found (not the payer, or unknown), payment_not_refundable,
    refund_exceeds_refundable, refund_already_requested.
    """
    with session_factory.begin() as session:
        payment = lock_payment(session, payment_id, payer_user_id=user_id)
        if payment is None:
            raise RefundRequestError("payment_not_found")
        available = refundable_minor(payment)
        if payment.state != "succeeded" or available <= 0:
            raise RefundRequestError("payment_not_refundable")
        requested = available if amount_minor is None else amount_minor
        if requested > available:
            raise RefundRequestError("refund_exceeds_refundable")
        payload: dict[str, object] = {"requested_minor": requested, "currency": payment.currency}
        if note:
            payload["note"] = note
        created = add_notification(
            session,
            clock(),
            "refund_requested",
            "merchant",
            payment.merchant_id,
            payment.id,
            payment.id,
            payload,
        )
        if not created:
            raise RefundRequestError("refund_already_requested")
        return RefundRequestResult(payment_id=payment.id, requested_minor=requested)


def _consumer_payment(payment: Payment, card_bin: str | None, card_last4: str | None) -> ConsumerPayment:
    return ConsumerPayment(
        payment_id=payment.id,
        merchant_id=payment.merchant_id,
        state=payment.state,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        card=None if card_bin is None or card_last4 is None else CardSummary(bin=card_bin, last4=card_last4),
        decline_code=payment.decline_code,
        refunded_minor=payment.refunded_minor,
        disputed_minor=payment.disputed_minor,
        chargeback_minor=payment.chargeback_minor,
        chargeback_flag=payment.chargeback_flag,
        created_at=payment.created_at,
    )
