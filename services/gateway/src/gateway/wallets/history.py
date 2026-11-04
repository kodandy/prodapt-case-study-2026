import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import or_, select, tuple_
from sqlalchemy.orm import Session, aliased

from gateway.accounts.models import User
from gateway.payments.history import DEFAULT_PAGE_SIZE, decode_cursor, encode_cursor
from gateway.wallets.models import Transfer


class TransferHistoryItem(BaseModel):
    """Carries no user or account ids and only the masked counterparty email (spec web P-1)."""

    transfer_id: uuid.UUID
    direction: Literal["sent", "received"]
    counterparty_email_masked: str
    amount_minor: int
    currency: str
    created_at: datetime


@dataclass(frozen=True)
class TransferPage:
    items: list[TransferHistoryItem]
    next_cursor: str | None


def list_transfers(
    session: Session, user_id: uuid.UUID, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
) -> TransferPage:
    """The user's sent and received transfers, newest first (`created_at`, then `id`, both DESC),
    keyset-paginated with the same cursor as `list_payments`. Raises InvalidCursorError."""
    sender = aliased(User)
    recipient = aliased(User)
    query = (
        select(Transfer, sender.email, recipient.email)
        .join(sender, sender.id == Transfer.sender_user_id)
        .join(recipient, recipient.id == Transfer.recipient_user_id)
        .where(or_(Transfer.sender_user_id == user_id, Transfer.recipient_user_id == user_id))
    )
    if cursor is not None:
        created_at, transfer_id = decode_cursor(cursor)
        query = query.where(tuple_(Transfer.created_at, Transfer.id) < tuple_(created_at, transfer_id))
    rows = session.execute(
        query.order_by(Transfer.created_at.desc(), Transfer.id.desc()).limit(limit + 1)
    ).all()
    page = rows[:limit]
    items = [
        _item(row, user_id, sender_email, recipient_email) for row, sender_email, recipient_email in page
    ]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return TransferPage(items=items, next_cursor=next_cursor)


def mask_email(email: str) -> str:
    """`alice@example.com` → `a***@example.com`: the first character and the part after the last `@`.
    A value with no `@` or an empty local part gives `***`, so it never echoes the stored value."""
    local, _, domain = email.rpartition("@")
    if not local:
        return "***"
    return f"{local[:1]}***@{domain}"


def _item(row: Transfer, user_id: uuid.UUID, sender_email: str, recipient_email: str) -> TransferHistoryItem:
    sent = row.sender_user_id == user_id
    return TransferHistoryItem(
        transfer_id=row.id,
        direction="sent" if sent else "received",
        counterparty_email_masked=mask_email(recipient_email if sent else sender_email),
        amount_minor=row.amount_minor,
        currency=row.currency,
        created_at=row.created_at,
    )
