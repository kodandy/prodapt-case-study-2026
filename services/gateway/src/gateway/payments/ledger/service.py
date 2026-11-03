import uuid
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gateway.clock import Clock
from gateway.payments.ledger.models import JournalEntry, Posting
from gateway.wallets.models import LedgerAccount


@dataclass(frozen=True)
class PostingLine:
    account_id: uuid.UUID
    currency: str
    amount_minor: int
    """Debit > 0, credit < 0."""


class UnbalancedJournalError(Exception):
    pass


def ensure_account(
    session: Session, owner_type: str, owner_id: uuid.UUID | None, kind: str, currency: str
) -> uuid.UUID:
    """Returns the ledger account id, creating it if missing (race-safe)."""
    session.execute(
        insert(LedgerAccount)
        .values(id=uuid.uuid4(), owner_type=owner_type, owner_id=owner_id, kind=kind, currency=currency)
        .on_conflict_do_nothing()
    )
    owner_match = LedgerAccount.owner_id.is_(None) if owner_id is None else LedgerAccount.owner_id == owner_id
    return session.scalars(
        select(LedgerAccount.id).where(
            LedgerAccount.owner_type == owner_type,
            owner_match,
            LedgerAccount.kind == kind,
            LedgerAccount.currency == currency,
        )
    ).one()


def post_journal(
    session: Session, clock: Clock, ref_type: str, ref_id: uuid.UUID, lines: list[PostingLine]
) -> uuid.UUID:
    """Writes one journal. Raises UnbalancedJournalError unless lines sum to zero per currency. No commit."""
    if not lines:
        raise UnbalancedJournalError("a journal needs at least one line")
    totals: dict[str, int] = defaultdict(int)
    for line in lines:
        totals[line.currency] += line.amount_minor
    unbalanced = {currency: total for currency, total in totals.items() if total != 0}
    if unbalanced:
        raise UnbalancedJournalError(f"unbalanced per currency: {unbalanced}")
    journal = JournalEntry(id=uuid.uuid4(), ref_type=ref_type, ref_id=ref_id, created_at=clock())
    session.add(journal)
    session.flush()
    session.add_all(
        Posting(
            id=uuid.uuid4(),
            journal_id=journal.id,
            account_id=line.account_id,
            currency=line.currency,
            amount_minor=line.amount_minor,
        )
        for line in lines
    )
    session.flush()
    return journal.id
