import re
import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from contracts.events import (
    DISPUTES_TOPIC,
    NETWORK_REASON_CODE_PATTERN,
    DisputeActorRole,
    DisputeEventState,
    DisputeEventType,
    DisputeEventV1,
)
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.notifications.records import add_notification
from gateway.outbox.models import OutboxEvent
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.payments.models import Dispute, Payment
from gateway.payments.refunds import lock_payment, refundable_minor
from gateway.payments.schemas import DisputeView

MAX_REASON_LENGTH = 64

CHARGEBACK_FEE_MINOR = {"USD": 1500, "EUR": 1500, "JPY": 2000, "INR": 120000}

DisputeOutcome = Literal["won", "lost"]

_EVENT_VERSIONS: dict[DisputeEventState, int] = {"opened": 1, "under_review": 2, "won": 3, "lost": 3}
_NETWORK_REASON_CODE = re.compile(NETWORK_REASON_CODE_PATTERN)


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
    *,
    payer_user_id: uuid.UUID | None = None,
    actor_id: str = "network",
    actor_role: str = "card_network",
) -> DisputeView:
    """Opens a dispute and reserves its amount on the payment (`disputed_minor`).

    With `payer_user_id` (consumer route) a payment the user did not pay raises PaymentNotFoundError, the same
    as an unknown one. The owning merchant gets a `dispute_opened` notification in the same transaction.
    """
    if not valid_dispute_reason(reason):
        raise InvalidDisputeReasonError
    with session_factory.begin() as session:
        return open_dispute_in_session(
            session,
            clock,
            payment_id,
            reason,
            amount_minor,
            payer_user_id=payer_user_id,
            actor_id=actor_id,
            actor_role=actor_role,
        )


def open_dispute_in_session(
    session: Session,
    clock: Clock,
    payment_id: uuid.UUID,
    reason: str,
    amount_minor: int | None = None,
    *,
    payer_user_id: uuid.UUID | None = None,
    actor_id: str = "network",
    actor_role: str = "card_network",
) -> DisputeView:
    """`open_dispute` inside the caller's transaction (the caller commits or rolls back)."""
    if not valid_dispute_reason(reason):
        raise InvalidDisputeReasonError
    payment = lock_payment(session, payment_id, payer_user_id=payer_user_id)
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
    _audit(session, clock, actor_id, actor_role, "dispute.opened", dispute, {"amount_minor": amount})
    _add_dispute_event(
        session,
        now,
        "dispute.opened",
        dispute,
        payment,
        (None, "opened"),
        "consumer" if actor_role == "consumer" else "card_network",
    )
    add_notification(
        session,
        now,
        "dispute_opened",
        "merchant",
        dispute.merchant_id,
        payment.id,
        dispute.id,
        _notice_payload(dispute),
    )
    return dispute_view(dispute)


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
        now = clock()
        dispute.state = "under_review"
        dispute.evidence_text = text
        dispute.updated_at = now
        session.flush()
        payment = session.get(Payment, dispute.payment_id)
        if payment is None:
            raise DisputeNotFoundError
        _add_dispute_event(
            session,
            now,
            "dispute.evidence_submitted",
            dispute,
            payment,
            ("opened", "under_review"),
            "merchant",
        )
        _audit(
            session,
            clock,
            f"merchant:{merchant_id}",
            "merchant_api",
            "dispute.evidence_submitted",
            dispute,
            {"evidence_chars": len(text)},
        )
        return dispute_view(dispute)


def resolve_dispute(
    session_factory: sessionmaker[Session],
    clock: Clock,
    dispute_id: uuid.UUID,
    outcome: DisputeOutcome,
    actor_id: str,
    actor_role: str = "ops_lead",
) -> DisputeView:
    """Network / ops_lead: `under_review → won | lost`. `lost` posts the chargeback and its fee and sets the
    payment's `chargeback_flag`. Notifies the merchant (`dispute_resolved`) and the payer, if any
    (`dispute_update`)."""
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
            payment.chargeback_flag = True
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
            actor_role,
            "dispute.resolved",
            dispute,
            {"outcome": outcome, "chargeback_fee_minor": dispute.chargeback_fee_minor},
        )
        _add_dispute_event(
            session,
            now,
            "dispute.resolved",
            dispute,
            payment,
            ("under_review", outcome),
            "card_network" if actor_role == "card_network" else "ops_lead",
        )
        payload = _notice_payload(dispute)
        add_notification(
            session, now, "dispute_resolved", "merchant", dispute.merchant_id, payment.id, dispute.id, payload
        )
        if payment.payer_user_id is not None:
            add_notification(
                session, now, "dispute_update", "user", payment.payer_user_id, payment.id, dispute.id, payload
            )
        return dispute_view(dispute)


def get_dispute(session: Session, merchant_id: uuid.UUID, dispute_id: uuid.UUID) -> DisputeView:
    """Raises DisputeNotFoundError for an unknown dispute or another merchant's."""
    dispute = session.get(Dispute, dispute_id)
    if dispute is None or dispute.merchant_id != merchant_id:
        raise DisputeNotFoundError
    return dispute_view(dispute)


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


def _add_dispute_event(
    session: Session,
    now: datetime,
    event_type: DisputeEventType,
    dispute: Dispute,
    payment: Payment,
    transition: tuple[DisputeEventState | None, DisputeEventState],
    actor_role: DisputeActorRole,
) -> None:
    """One gw.disputes.v1 outbox row in the caller's transaction (docs/specs/disputes-notifications.md).

    A free-text reason (consumer "not me", anything not shaped like a network code) is sent as None, and the
    evidence text never leaves the gateway: both can carry personal data.
    """
    from_state, to_state = transition
    lost = to_state == "lost"
    event = DisputeEventV1(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=now,
        entity_version=_EVENT_VERSIONS[to_state],
        dispute_id=dispute.id,
        payment_id=dispute.payment_id,
        merchant_id=dispute.merchant_id,
        from_state=from_state,
        to_state=to_state,
        actor_role=actor_role,
        network_reason_code=dispute.reason if _NETWORK_REASON_CODE.fullmatch(dispute.reason) else None,
        currency=dispute.currency,
        payment_amount_minor=payment.amount_minor,
        refunded_minor=payment.refunded_minor,
        dispute_amount_minor=dispute.amount_minor,
        chargeback_minor=dispute.amount_minor if lost else 0,
        chargeback_fee_minor=dispute.chargeback_fee_minor if lost else 0,
    )
    session.add(
        OutboxEvent(
            event_id=event.event_id,
            topic=DISPUTES_TOPIC,
            payload=event.model_dump(mode="json"),
            headers={},
            created_at=now,
            published_at=None,
        )
    )


def _notice_payload(dispute: Dispute) -> dict[str, object]:
    return {
        "dispute_id": str(dispute.id),
        "state": dispute.state,
        "amount_minor": dispute.amount_minor,
        "currency": dispute.currency,
    }


def dispute_view(dispute: Dispute) -> DisputeView:
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


def valid_dispute_reason(reason: str) -> bool:
    """1 to MAX_REASON_LENGTH chars, not blank, no Unicode Cc/Cf (NUL, newlines, zero-width, bidi)."""
    if not reason.strip() or len(reason) > MAX_REASON_LENGTH:
        return False
    return all(unicodedata.category(char) not in ("Cc", "Cf") for char in reason)
