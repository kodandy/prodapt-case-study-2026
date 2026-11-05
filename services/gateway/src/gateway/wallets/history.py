import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import Select, false, or_, select, tuple_
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


TRANSACTION_METHODS = frozenset({"p2p_transfer"})
TRANSACTION_STATUSES = frozenset({"succeeded"})


class TransactionItem(BaseModel):
    """A consumer's own money movement (B3.1). Today every one is a P2P transfer; like
    TransferHistoryItem it carries no user or account ids and only the masked counterparty email."""

    transaction_id: uuid.UUID
    method: Literal["p2p_transfer"]
    direction: Literal["sent", "received"]
    status: str
    counterparty_email_masked: str
    amount_minor: int
    currency: str
    created_at: datetime


@dataclass(frozen=True)
class TransactionPage:
    items: list[TransactionItem]
    next_cursor: str | None


_TransferRows = Select[Transfer, str, str]


def _own_transfers(user_id: uuid.UUID, cursor: str | None) -> _TransferRows:
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
    return query


def _page(
    session: Session, query: _TransferRows, limit: int
) -> tuple[list[tuple[Transfer, str, str]], str | None]:
    rows = session.execute(
        query.order_by(Transfer.created_at.desc(), Transfer.id.desc()).limit(limit + 1)
    ).all()
    page = [(row[0], row[1], row[2]) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1][0]
        next_cursor = encode_cursor(last.created_at, last.id)
    return page, next_cursor


def list_transfers(
    session: Session, user_id: uuid.UUID, limit: int = DEFAULT_PAGE_SIZE, cursor: str | None = None
) -> TransferPage:
    """The user's sent and received transfers, newest first (`created_at`, then `id`, both DESC),
    keyset-paginated with the same cursor as `list_payments`. Raises InvalidCursorError."""
    page, next_cursor = _page(session, _own_transfers(user_id, cursor), limit)
    items = [
        _item(row, user_id, sender_email, recipient_email) for row, sender_email, recipient_email in page
    ]
    return TransferPage(items=items, next_cursor=next_cursor)


def list_transactions(
    session: Session,
    user_id: uuid.UUID,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    method: str | None = None,
) -> TransactionPage:
    """The user's own transactions, newest first, keyset-paginated like `list_transfers`; the date range is
    half-open [created_from, created_to) and takes aware datetimes only. Raises InvalidCursorError, and
    ValueError for a naive bound."""
    for bound in (created_from, created_to):
        if bound is not None and bound.utcoffset() is None:
            raise ValueError("created_from / created_to must be timezone-aware")
    query = _own_transfers(user_id, cursor)
    if status is not None:
        query = query.where(Transfer.state == status)
    if method is not None and method not in TRANSACTION_METHODS:
        query = query.where(false())
    if created_from is not None:
        query = query.where(Transfer.created_at >= created_from)
    if created_to is not None:
        query = query.where(Transfer.created_at < created_to)
    page, next_cursor = _page(session, query, limit)
    items = [
        _transaction(row, user_id, sender_email, recipient_email)
        for row, sender_email, recipient_email in page
    ]
    return TransactionPage(items=items, next_cursor=next_cursor)


_UNPRINTABLE_CATEGORIES = frozenset({"C", "Z", "M"})


def mask_email(email: str) -> str:
    """`alice@example.com` → `a***@example.com`: the first character and the part after the last `@`.
    A value with no `@` or an empty local part gives `***`, so it never echoes the stored value. A first
    character that is a control, format (bidi), separator or combining mark becomes `*`, and a domain holding
    any such character becomes `***`: sign-up does not reject them, and RLO there would spoof the label."""
    local, _, domain = email.rpartition("@")
    if not local:
        return "***"
    first = "*" if _unprintable(local[0]) else local[0]
    shown_domain = "***" if any(_unprintable(ch) for ch in domain) else domain
    return f"{first}***@{shown_domain}"


def _unprintable(ch: str) -> bool:
    return unicodedata.category(ch)[0] in _UNPRINTABLE_CATEGORIES


def _item(row: Transfer, user_id: uuid.UUID, sender_email: str, recipient_email: str) -> TransferHistoryItem:
    sent = row.sender_user_id == user_id
    amount_minor, currency = row.amount_minor, row.currency
    # A cross-currency transfer shows each side its own money: what was debited or what was credited.
    if not sent and row.recipient_currency is not None and row.recipient_amount_minor is not None:
        amount_minor, currency = row.recipient_amount_minor, row.recipient_currency
    return TransferHistoryItem(
        transfer_id=row.id,
        direction="sent" if sent else "received",
        counterparty_email_masked=mask_email(recipient_email if sent else sender_email),
        amount_minor=amount_minor,
        currency=currency,
        created_at=row.created_at,
    )


def _transaction(
    row: Transfer, user_id: uuid.UUID, sender_email: str, recipient_email: str
) -> TransactionItem:
    transfer = _item(row, user_id, sender_email, recipient_email)
    return TransactionItem(
        transaction_id=transfer.transfer_id,
        method="p2p_transfer",
        direction=transfer.direction,
        status=row.state,
        counterparty_email_masked=transfer.counterparty_email_masked,
        amount_minor=transfer.amount_minor,
        currency=transfer.currency,
        created_at=transfer.created_at,
    )
