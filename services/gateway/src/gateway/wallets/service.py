import uuid

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.accounts.service import WALLET_KIND
from gateway.wallets.models import AccountBalance, LedgerAccount


class WalletBalanceView(BaseModel):
    account_id: uuid.UUID
    currency: str
    available_minor: int
    held_minor: int


def list_wallets(session: Session, user_id: uuid.UUID) -> list[WalletBalanceView]:
    """The user's wallet accounts with their balance rows, ordered by currency."""
    rows = session.execute(
        select(
            LedgerAccount.id,
            LedgerAccount.currency,
            AccountBalance.available_minor,
            AccountBalance.held_minor,
        )
        .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
        .where(
            LedgerAccount.owner_type == "user",
            LedgerAccount.owner_id == user_id,
            LedgerAccount.kind == WALLET_KIND,
        )
        .order_by(LedgerAccount.currency)
    ).all()
    return [
        WalletBalanceView(
            account_id=row.id,
            currency=row.currency,
            available_minor=row.available_minor,
            held_minor=row.held_minor,
        )
        for row in rows
    ]
