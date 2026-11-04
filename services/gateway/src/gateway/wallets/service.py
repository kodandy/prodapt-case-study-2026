import hashlib
import json
import uuid
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import User
from gateway.accounts.service import WALLET_KIND
from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.wallets.models import AccountBalance, LedgerAccount, Transfer

TOPUP_CLEARING_KIND = "topup_clearing"


class WalletBalanceView(BaseModel):
    account_id: uuid.UUID
    currency: str
    available_minor: int
    held_minor: int


@dataclass(frozen=True)
class TransferOutcome:
    status_code: int
    """201 transferred or replayed, 404 recipient_not_found, 409 insufficient_funds, 422 self / key reused."""
    body: dict[str, object]


RECIPIENT_NOT_FOUND = TransferOutcome(404, {"error": "recipient_not_found"})
SELF_TRANSFER = TransferOutcome(422, {"error": "self_transfer"})
KEY_REUSED = TransferOutcome(422, {"error": "idempotency_key_reused"})


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


def ensure_wallet(session: Session, user_id: uuid.UUID, currency: str) -> uuid.UUID:
    """The user's wallet account in `currency` with its balance row, created (zero) if missing. Race-safe."""
    account_id = ensure_account(session, "user", user_id, WALLET_KIND, currency)
    session.execute(
        insert(AccountBalance)
        .values(account_id=account_id, available_minor=0, held_minor=0)
        .on_conflict_do_nothing()
    )
    return account_id


def top_up_wallet(
    session: Session, clock: Clock, user_id: uuid.UUID, currency: str, amount_minor: int
) -> uuid.UUID:
    """Credits the wallet from the platform top-up clearing account. Test and demo seeding only. No commit."""
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    wallet_id = ensure_wallet(session, user_id, currency)
    balance = _lock_balances(session, [wallet_id])[wallet_id]
    balance.available_minor += amount_minor
    clearing = ensure_account(session, "platform", None, TOPUP_CLEARING_KIND, currency)
    post_journal(
        session,
        clock,
        "top_up",
        uuid.uuid4(),
        [
            PostingLine(clearing, currency, amount_minor),
            PostingLine(wallet_id, currency, -amount_minor),
        ],
    )
    return wallet_id


def transfer(
    session_factory: sessionmaker[Session],
    clock: Clock,
    sender_user_id: uuid.UUID,
    idempotency_key: str,
    recipient_email: str,
    amount_minor: int,
    currency: str,
) -> TransferOutcome:
    """Same-currency wallet-to-wallet transfer in one transaction under both balance-row locks.

    Same key + same request → the stored body; same key + different request → 422. Errors are not stored.
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    email = recipient_email.strip().lower()
    request_hash = _request_hash(email, amount_minor, currency)
    try:
        return _transfer_in_tx(
            session_factory,
            clock,
            sender_user_id,
            idempotency_key,
            email,
            amount_minor,
            currency,
            request_hash,
        )
    except _RejectedError as rejected:
        return rejected.outcome
    except _KeyTakenConcurrentlyError:
        with session_factory() as session:
            stored = _stored(session, sender_user_id, idempotency_key, request_hash)
        return KEY_REUSED if stored is None else stored


class _RejectedError(Exception):
    """Rolls back anything the attempt created (a recipient wallet) before answering with `outcome`."""

    def __init__(self, outcome: TransferOutcome) -> None:
        super().__init__(outcome.body)
        self.outcome = outcome


class _KeyTakenConcurrentlyError(Exception):
    pass


def _transfer_in_tx(
    session_factory: sessionmaker[Session],
    clock: Clock,
    sender_user_id: uuid.UUID,
    idempotency_key: str,
    email: str,
    amount_minor: int,
    currency: str,
    request_hash: str,
) -> TransferOutcome:
    with session_factory.begin() as session:
        replay = _stored(session, sender_user_id, idempotency_key, request_hash)
        if replay is not None:
            return replay
        recipient_user_id = session.scalar(select(User.id).where(User.email == email))
        if recipient_user_id is None:
            return RECIPIENT_NOT_FOUND
        if recipient_user_id == sender_user_id:
            return SELF_TRANSFER
        sender_account_id = _wallet_id(session, sender_user_id, currency)
        if sender_account_id is None:
            return _insufficient(0)
        recipient_account_id = ensure_wallet(session, recipient_user_id, currency)
        balances = _lock_balances(session, [sender_account_id, recipient_account_id])
        # A same-key request that committed while this one waited for the locks must answer, not a 409.
        replay = _stored(session, sender_user_id, idempotency_key, request_hash)
        if replay is not None:
            raise _RejectedError(replay)
        sender_balance = balances[sender_account_id]
        if sender_balance.available_minor < amount_minor:
            raise _RejectedError(_insufficient(sender_balance.available_minor))
        sender_balance.available_minor -= amount_minor
        balances[recipient_account_id].available_minor += amount_minor
        now = clock()
        transfer_id = uuid.uuid4()
        body: dict[str, object] = {
            "transfer_id": str(transfer_id),
            "state": "succeeded",
            "amount_minor": amount_minor,
            "currency": currency,
            "recipient_user_id": str(recipient_user_id),
            "sender_available_minor": sender_balance.available_minor,
        }
        inserted = session.execute(
            insert(Transfer)
            .values(
                id=transfer_id,
                sender_user_id=sender_user_id,
                recipient_user_id=recipient_user_id,
                sender_account_id=sender_account_id,
                recipient_account_id=recipient_account_id,
                amount_minor=amount_minor,
                currency=currency,
                state="succeeded",
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_body=body,
                created_at=now,
            )
            .on_conflict_do_nothing()
            .returning(Transfer.id)
        ).scalar_one_or_none()
        if inserted is None:
            raise _KeyTakenConcurrentlyError
        post_journal(
            session,
            clock,
            "transfer",
            transfer_id,
            [
                PostingLine(sender_account_id, currency, amount_minor),
                PostingLine(recipient_account_id, currency, -amount_minor),
            ],
        )
        session.add(
            AuditLog(
                actor_id=f"user:{sender_user_id}",
                actor_role="consumer",
                action="transfer.created",
                target=f"transfer:{transfer_id}",
                summary={
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "recipient_user_id": str(recipient_user_id),
                },
                correlation_id=uuid.uuid4().hex,
                occurred_at=now,
            )
        )
        return TransferOutcome(201, body)


def _insufficient(available_minor: int) -> TransferOutcome:
    return TransferOutcome(409, {"error": "insufficient_funds", "available_minor": available_minor})


def _wallet_id(session: Session, user_id: uuid.UUID, currency: str) -> uuid.UUID | None:
    return session.scalar(
        select(LedgerAccount.id)
        .join(AccountBalance, AccountBalance.account_id == LedgerAccount.id)
        .where(
            LedgerAccount.owner_type == "user",
            LedgerAccount.owner_id == user_id,
            LedgerAccount.kind == WALLET_KIND,
            LedgerAccount.currency == currency,
        )
    )


def _lock_balances(session: Session, account_ids: list[uuid.UUID]) -> dict[uuid.UUID, AccountBalance]:
    """`SELECT … FOR UPDATE` in ascending account id order so opposite transfers cannot deadlock."""
    rows = session.scalars(
        select(AccountBalance)
        .where(AccountBalance.account_id.in_(account_ids))
        .order_by(AccountBalance.account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {row.account_id: row for row in rows}


def _request_hash(email: str, amount_minor: int, currency: str) -> str:
    canonical = {"recipient_email": email, "amount_minor": amount_minor, "currency": currency}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _stored(
    session: Session, sender_user_id: uuid.UUID, idempotency_key: str, request_hash: str
) -> TransferOutcome | None:
    row = session.execute(
        select(Transfer.request_hash, Transfer.response_body).where(
            Transfer.sender_user_id == sender_user_id, Transfer.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        return KEY_REUSED
    return TransferOutcome(201, row.response_body)
