import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, select, tuple_
from sqlalchemy.orm import Session

from gateway.payments.disputes import PaymentNotFoundError
from gateway.payments.models import Payment
from gateway.payments.schemas import CardSummary, PaymentSummary
from gateway.payments.state import PaymentState
from gateway.payments.tokenization.models import CardToken

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class InvalidCursorError(Exception):
    pass


@dataclass(frozen=True)
class PaymentPage:
    items: list[PaymentSummary]
    next_cursor: str | None


def list_payments(
    session: Session,
    merchant_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    state: PaymentState | None = None,
) -> PaymentPage:
    """The merchant's payments, newest first (`created_at`, then `id`, both DESC), keyset-paginated."""
    query = _summary_query(merchant_id)
    if state is not None:
        query = query.where(Payment.state == state)
    if cursor is not None:
        created_at, payment_id = decode_cursor(cursor)
        query = query.where(tuple_(Payment.created_at, Payment.id) < tuple_(created_at, payment_id))
    rows = session.execute(
        query.order_by(Payment.created_at.desc(), Payment.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    items = [_summary(payment, card_bin, card_last4) for payment, card_bin, card_last4 in page]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return PaymentPage(items=items, next_cursor=next_cursor)


def get_payment(session: Session, merchant_id: uuid.UUID, payment_id: uuid.UUID) -> PaymentSummary:
    """The same summary as a `list_payments` item. Another merchant's payment and an unknown id both raise
    PaymentNotFoundError, so a caller cannot tell them apart."""
    row = session.execute(_summary_query(merchant_id).where(Payment.id == payment_id)).one_or_none()
    if row is None:
        raise PaymentNotFoundError
    payment, card_bin, card_last4 = row
    return _summary(payment, card_bin, card_last4)


def _summary_query(merchant_id: uuid.UUID) -> Select[Payment, str, str]:
    return (
        select(Payment, CardToken.bin, CardToken.last4)
        .outerjoin(CardToken, CardToken.token == Payment.card_token)
        .where(Payment.merchant_id == merchant_id)
    )


def _summary(payment: Payment, card_bin: str | None, card_last4: str | None) -> PaymentSummary:
    return PaymentSummary(
        payment_id=payment.id,
        state=payment.state,
        amount_minor=payment.amount_minor,
        currency=payment.currency,
        method=payment.method,
        card=None if card_bin is None or card_last4 is None else CardSummary(bin=card_bin, last4=card_last4),
        decline_code=payment.decline_code,
        refunded_minor=payment.refunded_minor,
        disputed_minor=payment.disputed_minor,
        chargeback_minor=payment.chargeback_minor,
        created_at=payment.created_at,
    )


def encode_cursor(created_at: datetime, payment_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(
        f"{created_at.astimezone(UTC).isoformat()}|{payment_id}".encode()
    ).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_text, id_text = raw.split("|")
        created_at = datetime.fromisoformat(created_text)
        payment_id = uuid.UUID(id_text)
    except (binascii.Error, UnicodeError, ValueError, OverflowError):
        raise InvalidCursorError from None
    if created_at.utcoffset() != timedelta(0):
        raise InvalidCursorError
    return created_at, payment_id
