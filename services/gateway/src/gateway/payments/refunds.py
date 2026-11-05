import hashlib
import json
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.payments.fx.service import journal_fx_rate, reversal_lines
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.payments.models import Payment, Refund


@dataclass(frozen=True)
class RefundOutcome:
    status_code: int
    """201 refunded (or replayed), 404 payment_not_found, 409 not refundable / exceeds, 422 key reused."""
    body: dict[str, object]


PAYMENT_NOT_FOUND = RefundOutcome(404, {"error": "payment_not_found"})
KEY_REUSED = RefundOutcome(422, {"error": "idempotency_key_reused"})
NOT_REFUNDABLE = RefundOutcome(409, {"error": "payment_not_refundable"})


def refundable_minor(payment: Payment) -> int:
    """What a refund or a new dispute may still take from the payment."""
    return payment.amount_minor - payment.refunded_minor - payment.disputed_minor - payment.chargeback_minor


def lock_payment(
    session: Session, payment_id: uuid.UUID, merchant_id: uuid.UUID | None = None
) -> Payment | None:
    """`SELECT … FOR UPDATE` on the payment row: refunds and dispute changes serialize here.

    With `merchant_id`, another merchant's payment is neither returned nor locked.
    """
    query = select(Payment).where(Payment.id == payment_id)
    if merchant_id is not None:
        query = query.where(Payment.merchant_id == merchant_id)
    return session.scalars(query.with_for_update().execution_options(populate_existing=True)).one_or_none()


def refund_payment(
    session_factory: sessionmaker[Session],
    clock: Clock,
    merchant_id: uuid.UUID,
    payment_id: uuid.UUID,
    idempotency_key: str,
    amount_minor: int | None,
) -> RefundOutcome:
    """Full (`amount_minor` None) or partial refund of a succeeded payment, with its reversal journal.

    Same key + same request → the stored body; same key + different request → 422. Errors are not stored.
    """
    request_hash = _request_hash(payment_id, amount_minor)
    try:
        return _refund_in_tx(
            session_factory, clock, merchant_id, payment_id, idempotency_key, amount_minor, request_hash
        )
    except _KeyTakenConcurrentlyError:
        with session_factory() as session:
            return _replay_or(session, merchant_id, idempotency_key, request_hash, KEY_REUSED)


class _KeyTakenConcurrentlyError(Exception):
    """The key was taken concurrently for another payment, which this payment's lock does not cover."""


def _refund_in_tx(
    session_factory: sessionmaker[Session],
    clock: Clock,
    merchant_id: uuid.UUID,
    payment_id: uuid.UUID,
    idempotency_key: str,
    amount_minor: int | None,
    request_hash: str,
) -> RefundOutcome:
    with session_factory.begin() as session:
        payment = lock_payment(session, payment_id, merchant_id)
        if payment is None:
            return _replay_or(session, merchant_id, idempotency_key, request_hash, PAYMENT_NOT_FOUND)
        replay = _stored(session, merchant_id, idempotency_key, request_hash)
        if replay is not None:
            return replay
        if payment.state != "succeeded":
            return NOT_REFUNDABLE
        available = refundable_minor(payment)
        amount = available if amount_minor is None else amount_minor
        if available == 0 or amount > available:
            return RefundOutcome(409, {"error": "refund_exceeds_refundable", "refundable_minor": available})
        now = clock()
        refund_id = uuid.uuid4()
        payment.refunded_minor += amount
        payment.updated_at = now
        body: dict[str, object] = {
            "refund_id": str(refund_id),
            "payment_id": str(payment.id),
            "state": "succeeded",
            "amount_minor": amount,
            "currency": payment.currency,
            "payment_refunded_minor": payment.refunded_minor,
            "payment_refundable_minor": refundable_minor(payment),
        }
        inserted = session.execute(
            insert(Refund)
            .values(
                id=refund_id,
                payment_id=payment.id,
                merchant_id=merchant_id,
                amount_minor=amount,
                currency=payment.currency,
                state="succeeded",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_body=body,
                created_at=now,
            )
            .on_conflict_do_nothing()
            .returning(Refund.id)
        ).scalar_one_or_none()
        if inserted is None:
            raise _KeyTakenConcurrentlyError
        receivable = ensure_account(session, "platform", None, "provider_receivable", payment.currency)
        fx_rate = journal_fx_rate(session, "payment", payment.id)
        if fx_rate is not None:
            settled_payable = ensure_account(
                session, "merchant", merchant_id, "merchant_payable", fx_rate.quote_currency
            )
            already_refunded = payment.refunded_minor - amount
            lines = reversal_lines(session, fx_rate, already_refunded, amount, settled_payable, receivable)
            post_journal(session, clock, "refund", refund_id, lines, fx_rate_id=fx_rate.id)
        else:
            payable = ensure_account(session, "merchant", merchant_id, "merchant_payable", payment.currency)
            post_journal(
                session,
                clock,
                "refund",
                refund_id,
                [
                    PostingLine(payable, payment.currency, amount),
                    PostingLine(receivable, payment.currency, -amount),
                ],
            )
        session.add(
            AuditLog(
                actor_id=f"merchant:{merchant_id}",
                actor_role="merchant_api",
                action="payment.refund",
                target=f"payment:{payment.id}",
                summary={"refund_id": str(refund_id), "amount_minor": amount, "currency": payment.currency},
                correlation_id=uuid.uuid4().hex,
                occurred_at=now,
            )
        )
        return RefundOutcome(201, body)


def _request_hash(payment_id: uuid.UUID, amount_minor: int | None) -> str:
    canonical = {"payment_id": str(payment_id), "amount_minor": amount_minor}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stored(
    session: Session, merchant_id: uuid.UUID, idempotency_key: str, request_hash: str
) -> RefundOutcome | None:
    row = session.execute(
        select(Refund.request_hash, Refund.response_body).where(
            Refund.merchant_id == merchant_id, Refund.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        return KEY_REUSED
    return RefundOutcome(201, row.response_body)


def _replay_or(
    session: Session, merchant_id: uuid.UUID, idempotency_key: str, request_hash: str, fallback: RefundOutcome
) -> RefundOutcome:
    stored = _stored(session, merchant_id, idempotency_key, request_hash)
    return fallback if stored is None else stored
