import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.scope import UserScope
from ops_ai.transfers.models import TransferFact

CauseCode = Literal[
    "cross_border_limit_for_kyc_tier",
    "insufficient_funds",
    "kyc_verification_failed",
    "not_declined",
    "unclassified",
]
RemediationOwner = Literal["sender", "transfer_provider", "unknown"]
SenderStatus = Literal["success", "failed", "pending", "reversed"]

_SENDER_FACING_STATUS: dict[str, SenderStatus] = {
    "success": "success",
    "failed": "failed",
    "pending": "pending",
    "reversed": "reversed",
    "flagged": "pending",
}


@dataclass(frozen=True)
class TransferStatus:
    """A transfer as the sender may see it: no `rule_id`. `status` is the raw value; show the sender
    `sender_facing_status(status)`."""

    transfer_id: uuid.UUID
    amount_minor: int
    currency: str
    method: str
    status: str
    country_sender: str
    country_receiver: str
    reason_code: str | None
    kyc_tier: int | None
    limit_minor: int | None
    limit_currency: str | None
    next_tier_limit_minor: int | None
    sender_balance_minor: int | None
    decided_at: datetime


def sender_facing_status(raw: str) -> SenderStatus:
    """The status the sender is shown. A fraud hold (`flagged`) and any unknown value read as `pending`, so
    the sender cannot tell a hold apart or probe its thresholds."""
    return _SENDER_FACING_STATUS.get(raw, "pending")


def get_payment_status(
    session: Session, scope: UserScope, transfer_id: uuid.UUID | None
) -> TransferStatus | None:
    """The scope's own transfer by id, or its newest failed transfer when `transfer_id` is None.

    Another sender's id and an unknown id are both None.
    """
    query = select(TransferFact).where(TransferFact.sender_user_id == scope.user_id)
    if transfer_id is None:
        query = query.where(TransferFact.status == "failed").order_by(
            TransferFact.decided_at.desc(), TransferFact.transfer_id
        )
    else:
        query = query.where(TransferFact.transfer_id == transfer_id)
    fact = session.scalars(query.limit(1)).one_or_none()
    if fact is None:
        return None
    return TransferStatus(
        transfer_id=fact.transfer_id,
        amount_minor=fact.amount_minor,
        currency=fact.currency,
        method=fact.method,
        status=fact.status,
        country_sender=fact.country_sender,
        country_receiver=fact.country_receiver,
        reason_code=fact.reason_code,
        kyc_tier=fact.kyc_tier,
        limit_minor=fact.limit_minor,
        limit_currency=fact.limit_currency,
        next_tier_limit_minor=fact.next_tier_limit_minor,
        sender_balance_minor=fact.sender_balance_minor,
        decided_at=fact.decided_at,
    )


def classify_transfer(status: TransferStatus) -> tuple[CauseCode, RemediationOwner]:
    """Names a cause only when the reason code and the recorded numbers agree; first match wins."""
    if status.status != "failed":
        return "not_declined", "unknown"
    amount, balance = status.amount_minor, status.sender_balance_minor
    if (
        status.reason_code == "cross_border_limit_kyc_tier"
        and status.country_sender != status.country_receiver
        and status.limit_currency == status.currency
        and status.kyc_tier is not None
        and status.limit_minor is not None
        and balance is not None
        and amount > status.limit_minor
        and balance >= amount
    ):
        return "cross_border_limit_for_kyc_tier", "sender"
    if status.reason_code == "insufficient_funds" and balance is not None and balance < amount:
        return "insufficient_funds", "sender"
    if status.reason_code == "kyc_verification_failed":
        return "kyc_verification_failed", "sender"
    return "unclassified", "unknown"
