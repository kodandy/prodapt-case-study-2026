import unicodedata
import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.payments.models import Dispute
from gateway.payments.refunds import lock_payment, refundable_minor
from gateway.payments.schemas import DisputeView

MAX_REASON_LENGTH = 64

CHARGEBACK_FEE_MINOR = {"USD": 1500, "EUR": 1500, "JPY": 2000, "INR": 120000}

DisputeOutcome = Literal["won", "lost"]


class PaymentNotFoundError(Exception):
    pass


class PaymentNotDisputableError(Exception):
    pass


class DisputeAlreadyOpenError(Exception):
    pass


class DisputeAmountExceedsError(Exception):
    pass


class InvalidDisputeReasonError(Exception):
    pass


class DisputeNotFoundError(Exception):
    pass


class InvalidDisputeTransitionError(Exception):
    pass


def open_dispute(
    session_factory: sessionmaker[Session],
    clock: Clock,
    payment_id: uuid.UUID,
    reason: str,
    amount_minor: int | None = None,
) -> DisputeView:
    """Network-side: opens a dispute and reserves its amount on the payment (`disputed_minor`)."""
    if not _valid_reason(reason):
        raise InvalidDisputeReasonError
    with session_factory.begin() as session:
        payment = lock_payment(session, payment_id)
        if payment is None:
            raise PaymentNotFoundError
        if payment.state != "succeeded":
            raise PaymentNotDisputableError
        active = session.scalar(
            select(Dispute.id).where(
                Dispute.payment_id == payment_id, Dispute.state.in_(("opened", "under_review"))
            )
        )
        if active is not None:
            raise DisputeAlreadyOpenError
        available = refundable_minor(payment)
        amount = available if amount_minor is None else amount_minor
        if amount <= 0 or amount > available:
            raise DisputeAmountExceedsError
        now = clock()
        dispute = Dispute(
            id=uuid.uuid4(),
            payment_id=payment.id,
            merchant_id=payment.merchant_id,
            amount_minor=amount,
            currency=payment.currency,
            reason=reason,
            state="opened",
            evidence_text=None,
            chargeback_fee_minor=0,
            created_at=now,
            updated_at=now,
            resolved_at=None,
        )
        session.add(dispute)
        payment.disputed_minor += amount
        payment.updated_at = now
        session.flush()
        _audit(session, clock, "network", "card_network", "dispute.opened", dispute, {"amount_minor": amount})
        return _view(dispute)


def submit_evidence(
    session_factory: sessionmaker[Session],
    clock: Clock,
    merchant_id: uuid.UUID,
    dispute_id: uuid.UUID,
    text: str,
) -> DisputeView:
    """Merchant: `opened → under_review` with the evidence text."""
    with session_factory.begin() as session:
        dispute = _lock_dispute(session, dispute_id, merchant_id)
        if dispute is None:
            raise DisputeNotFoundError
        if dispute.state != "opened":
            raise InvalidDisputeTransitionError
        dispute.state = "under_review"
        dispute.evidence_text = text
        dispute.updated_at = clock()
        session.flush()
        _audit(
            session,
            clock,
            f"merchant:{merchant_id}",
            "merchant_api",
            "dispute.evidence_submitted",
            dispute,
            {"evidence_chars": len(text)},
        )
        return _view(dispute)


def resolve_dispute(
    session_factory: sessionmaker[Session],
    clock: Clock,
    dispute_id: uuid.UUID,
    outcome: DisputeOutcome,
    actor_id: str,
) -> DisputeView:
    """Network / ops_lead: `under_review → won | lost`. `lost` posts the chargeback and its fee."""
    if outcome not in ("won", "lost"):
        raise ValueError(f"unknown dispute outcome: {outcome!r}")
    with session_factory.begin() as session:
        dispute = _lock_dispute(session, dispute_id)
        if dispute is None:
            raise DisputeNotFoundError
        payment = lock_payment(session, dispute.payment_id)
        if payment is None:
            raise DisputeNotFoundError
        if dispute.state != "under_review":
            raise InvalidDisputeTransitionError
        now = clock()
        payment.disputed_minor -= dispute.amount_minor
        if outcome == "lost":
            fee = CHARGEBACK_FEE_MINOR[dispute.currency]
            payment.chargeback_minor += dispute.amount_minor
            dispute.chargeback_fee_minor = fee
            _post_chargeback(session, clock, dispute, fee)
        payment.updated_at = now
        dispute.state = outcome
        dispute.updated_at = now
        dispute.resolved_at = now
        session.flush()
        _audit(
            session,
            clock,
            actor_id,
            "ops_lead",
            "dispute.resolved",
            dispute,
            {"outcome": outcome, "chargeback_fee_minor": dispute.chargeback_fee_minor},
        )
        return _view(dispute)


def get_dispute(session: Session, merchant_id: uuid.UUID, dispute_id: uuid.UUID) -> DisputeView:
    """Raises DisputeNotFoundError for an unknown dispute or another merchant's."""
    dispute = session.get(Dispute, dispute_id)
    if dispute is None or dispute.merchant_id != merchant_id:
        raise DisputeNotFoundError
    return _view(dispute)


def _lock_dispute(
    session: Session, dispute_id: uuid.UUID, merchant_id: uuid.UUID | None = None
) -> Dispute | None:
    query = select(Dispute).where(Dispute.id == dispute_id)
    if merchant_id is not None:
        query = query.where(Dispute.merchant_id == merchant_id)
    return session.scalars(query.with_for_update()).one_or_none()


def _post_chargeback(session: Session, clock: Clock, dispute: Dispute, fee: int) -> None:
    currency = dispute.currency
    payable = ensure_account(session, "merchant", dispute.merchant_id, "merchant_payable", currency)
    receivable = ensure_account(session, "platform", None, "provider_receivable", currency)
    fee_revenue = ensure_account(session, "platform", None, "chargeback_fee_revenue", currency)
    post_journal(
        session,
        clock,
        "dispute",
        dispute.id,
        [
            PostingLine(payable, currency, dispute.amount_minor),
            PostingLine(receivable, currency, -dispute.amount_minor),
            PostingLine(payable, currency, fee),
            PostingLine(fee_revenue, currency, -fee),
        ],
    )


def _audit(
    session: Session,
    clock: Clock,
    actor_id: str,
    actor_role: str,
    action: str,
    dispute: Dispute,
    summary: dict[str, object],
) -> None:
    session.add(
        AuditLog(
            actor_id=actor_id[:64],
            actor_role=actor_role,
            action=action,
            target=f"dispute:{dispute.id}",
            summary={"payment_id": str(dispute.payment_id), "state": dispute.state, **summary},
            correlation_id=uuid.uuid4().hex,
            occurred_at=clock(),
        )
    )


def _view(dispute: Dispute) -> DisputeView:
    return DisputeView(
        dispute_id=dispute.id,
        payment_id=dispute.payment_id,
        state=dispute.state,
        amount_minor=dispute.amount_minor,
        currency=dispute.currency,
        reason=dispute.reason,
        chargeback_fee_minor=dispute.chargeback_fee_minor,
        created_at=dispute.created_at,
        updated_at=dispute.updated_at,
    )


def _valid_reason(reason: str) -> bool:
    """1 to MAX_REASON_LENGTH chars, not blank, no Unicode Cc/Cf (NUL, newlines, zero-width, bidi)."""
    if not reason.strip() or len(reason) > MAX_REASON_LENGTH:
        return False
    return all(unicodedata.category(char) not in ("Cc", "Cf") for char in reason)
