import hashlib
import json
import uuid
from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import User
from gateway.accounts.service import WALLET_KIND
from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.payments.fx.service import FxAmountTooSmallError, FxQuote, FxRateNotFoundError, fx_lines, quote
from gateway.payments.ledger.models import Posting
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.wallets.models import AccountBalance, LedgerAccount, Transfer, WalletTopUp

TOPUP_CLEARING_KIND = "topup_clearing"
HOLD_KIND = "wallet_hold"
"""Per-user, per-currency ledger account for funds reserved by a pending payout (no balance row)."""
SUPPORTED_CURRENCIES = ("EUR", "INR", "JPY", "USD")
MAX_TOP_UP_MINOR = 10_000_000


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
FX_AMOUNT_TOO_SMALL = TransferOutcome(422, {"error": "fx_amount_too_small"})
FX_RATE_UNAVAILABLE = TransferOutcome(422, {"error": "fx_rate_unavailable"})


@dataclass(frozen=True)
class TopUpOutcome:
    status_code: int
    """201 credited or replayed, 422 idempotency_key_reused."""
    body: dict[str, object]


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


def create_wallet(session: Session, user_id: uuid.UUID, currency: str) -> tuple[WalletBalanceView, bool]:
    """The user's wallet in `currency` and whether this call created it. Race-safe. No commit."""
    account_id = ensure_account(session, "user", user_id, WALLET_KIND, currency)
    created = (
        session.execute(
            insert(AccountBalance)
            .values(account_id=account_id, available_minor=0, held_minor=0)
            .on_conflict_do_nothing()
            .returning(AccountBalance.account_id)
        ).scalar_one_or_none()
        is not None
    )
    balance = session.scalars(
        select(AccountBalance)
        .where(AccountBalance.account_id == account_id)
        .execution_options(populate_existing=True)
    ).one()
    view = WalletBalanceView(
        account_id=account_id,
        currency=currency,
        available_minor=balance.available_minor,
        held_minor=balance.held_minor,
    )
    return view, created


def ledger_wallet_balances(session: Session, user_id: uuid.UUID) -> list[WalletBalanceView]:
    """`list_wallets` recomputed from postings: available = -sum(wallet postings), held = -sum(holds)."""
    sums = {
        (row.kind, row.currency): int(row.total)
        for row in session.execute(
            select(LedgerAccount.kind, LedgerAccount.currency, func.sum(Posting.amount_minor).label("total"))
            .join(Posting, Posting.account_id == LedgerAccount.id)
            .where(
                LedgerAccount.owner_type == "user",
                LedgerAccount.owner_id == user_id,
                LedgerAccount.kind.in_((WALLET_KIND, HOLD_KIND)),
            )
            .group_by(LedgerAccount.kind, LedgerAccount.currency)
        )
    }
    wallets = session.execute(
        select(LedgerAccount.id, LedgerAccount.currency)
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
            available_minor=-sums.get((WALLET_KIND, row.currency), 0),
            held_minor=-sums.get((HOLD_KIND, row.currency), 0),
        )
        for row in wallets
    ]


def top_up_wallet(
    session: Session, clock: Clock, user_id: uuid.UUID, currency: str, amount_minor: int
) -> uuid.UUID:
    """Credits the wallet from the platform top-up clearing account. Test and demo seeding only. No commit."""
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    wallet_id = ensure_wallet(session, user_id, currency)
    _credit_from_top_up_clearing(session, clock, wallet_id, currency, amount_minor, uuid.uuid4())
    return wallet_id


def _credit_from_top_up_clearing(
    session: Session, clock: Clock, wallet_id: uuid.UUID, currency: str, amount_minor: int, ref_id: uuid.UUID
) -> int:
    balance = lock_balances(session, [wallet_id])[wallet_id]
    balance.available_minor += amount_minor
    clearing = ensure_account(session, "platform", None, TOPUP_CLEARING_KIND, currency)
    post_journal(
        session,
        clock,
        "top_up",
        ref_id,
        [
            PostingLine(clearing, currency, amount_minor),
            PostingLine(wallet_id, currency, -amount_minor),
        ],
    )
    return balance.available_minor


def top_up(
    session_factory: sessionmaker[Session],
    clock: Clock,
    user_id: uuid.UUID,
    idempotency_key: str,
    currency: str,
    amount_minor: int,
) -> TopUpOutcome:
    """Demo funding source: credits the wallet (created if missing) in one transaction.

    Same key + same request → the stored 201 body; same key + different request → 422.
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    request_hash = _hash({"currency": currency, "amount_minor": amount_minor})
    try:
        return _top_up_in_tx(
            session_factory, clock, user_id, idempotency_key, currency, amount_minor, request_hash
        )
    except _KeyTakenConcurrentlyError:
        with session_factory() as session:
            stored = _stored_top_up(session, user_id, idempotency_key, request_hash)
        return TOP_UP_KEY_REUSED if stored is None else stored


def _top_up_in_tx(
    session_factory: sessionmaker[Session],
    clock: Clock,
    user_id: uuid.UUID,
    idempotency_key: str,
    currency: str,
    amount_minor: int,
    request_hash: str,
) -> TopUpOutcome:
    with session_factory.begin() as session:
        replay = _stored_top_up(session, user_id, idempotency_key, request_hash)
        if replay is not None:
            return replay
        wallet_id = ensure_wallet(session, user_id, currency)
        lock_balances(session, [wallet_id])
        # A same-key request that committed while this one waited for the lock answers with its body.
        replay = _stored_top_up(session, user_id, idempotency_key, request_hash)
        if replay is not None:
            return replay
        now = clock()
        top_up_id = uuid.uuid4()
        available = _credit_from_top_up_clearing(session, clock, wallet_id, currency, amount_minor, top_up_id)
        body: dict[str, object] = {
            "top_up_id": str(top_up_id),
            "currency": currency,
            "amount_minor": amount_minor,
            "available_minor": available,
        }
        inserted = session.execute(
            insert(WalletTopUp)
            .values(
                id=top_up_id,
                user_id=user_id,
                account_id=wallet_id,
                amount_minor=amount_minor,
                currency=currency,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                response_body=body,
                created_at=now,
            )
            .on_conflict_do_nothing()
            .returning(WalletTopUp.id)
        ).scalar_one_or_none()
        if inserted is None:
            raise _KeyTakenConcurrentlyError
        session.add(
            AuditLog(
                actor_id=f"user:{user_id}",
                actor_role="consumer",
                action="wallet.top_up",
                target=f"top_up:{top_up_id}",
                summary={"amount_minor": amount_minor, "currency": currency},
                correlation_id=uuid.uuid4().hex,
                occurred_at=now,
            )
        )
        return TopUpOutcome(201, body)


TOP_UP_KEY_REUSED = TopUpOutcome(422, {"error": "idempotency_key_reused"})


def _stored_top_up(
    session: Session, user_id: uuid.UUID, idempotency_key: str, request_hash: str
) -> TopUpOutcome | None:
    row = session.execute(
        select(WalletTopUp.request_hash, WalletTopUp.response_body).where(
            WalletTopUp.user_id == user_id, WalletTopUp.idempotency_key == idempotency_key
        )
    ).one_or_none()
    if row is None:
        return None
    if row.request_hash != request_hash:
        return TOP_UP_KEY_REUSED
    return TopUpOutcome(201, dict(row.response_body))


def transfer(
    session_factory: sessionmaker[Session],
    clock: Clock,
    sender_user_id: uuid.UUID,
    idempotency_key: str,
    recipient_email: str,
    amount_minor: int,
    currency: str,
    recipient_currency: str | None = None,
) -> TransferOutcome:
    """Wallet-to-wallet transfer in one transaction under both balance-row locks.

    `recipient_currency` absent or equal to `currency`: same currency (B2). Otherwise the recipient's
    `recipient_currency` wallet is credited the converted amount (FX journal, rate id stored).
    Same key + same request → the stored body; same key + different request → 422. Errors are not stored.
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    if recipient_currency == currency:
        recipient_currency = None
    email = recipient_email.strip().lower()
    request_hash = _request_hash(email, amount_minor, currency, recipient_currency)
    try:
        return _transfer_in_tx(
            session_factory,
            clock,
            sender_user_id,
            idempotency_key,
            email,
            amount_minor,
            currency,
            recipient_currency,
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
    recipient_currency: str | None,
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
        fx_quote: FxQuote | None = None
        if recipient_currency is not None:
            try:
                fx_quote = quote(session, currency, recipient_currency, amount_minor, clock())
            except FxRateNotFoundError:
                return FX_RATE_UNAVAILABLE
            except FxAmountTooSmallError:
                return FX_AMOUNT_TOO_SMALL
        sender_account_id = find_wallet(session, sender_user_id, currency)
        if sender_account_id is None:
            return _insufficient(0)
        credit_currency = currency if fx_quote is None else fx_quote.target_currency
        credit_minor = amount_minor if fx_quote is None else fx_quote.target_minor
        recipient_account_id = ensure_wallet(session, recipient_user_id, credit_currency)
        balances = lock_balances(session, [sender_account_id, recipient_account_id])
        # A same-key request that committed while this one waited for the locks must answer, not a 409.
        replay = _stored(session, sender_user_id, idempotency_key, request_hash)
        if replay is not None:
            raise _RejectedError(replay)
        sender_balance = balances[sender_account_id]
        if sender_balance.available_minor < amount_minor:
            raise _RejectedError(_insufficient(sender_balance.available_minor))
        sender_balance.available_minor -= amount_minor
        balances[recipient_account_id].available_minor += credit_minor
        now = clock()
        transfer_id = uuid.uuid4()
        body: dict[str, object] = {
            "transfer_id": str(transfer_id),
            "state": "succeeded",
            "amount_minor": amount_minor,
            "currency": currency,
            "sender_available_minor": sender_balance.available_minor,
        }
        fx_fields: dict[str, object] = {}
        if fx_quote is not None:
            fx_fields = {
                "recipient_currency": fx_quote.target_currency,
                "recipient_amount_minor": fx_quote.target_minor,
                "fx_rate_id": fx_quote.rate_id,
            }
            body.update(fx_fields)
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
                recipient_currency=None if fx_quote is None else fx_quote.target_currency,
                recipient_amount_minor=None if fx_quote is None else fx_quote.target_minor,
                fx_rate_id=None if fx_quote is None else fx_quote.rate_id,
            )
            .on_conflict_do_nothing()
            .returning(Transfer.id)
        ).scalar_one_or_none()
        if inserted is None:
            raise _KeyTakenConcurrentlyError
        if fx_quote is None:
            lines = [
                PostingLine(sender_account_id, currency, amount_minor),
                PostingLine(recipient_account_id, currency, -amount_minor),
            ]
        else:
            lines = fx_lines(session, fx_quote, sender_account_id, recipient_account_id)
        post_journal(
            session,
            clock,
            "transfer",
            transfer_id,
            lines,
            fx_rate_id=None if fx_quote is None else fx_quote.rate_id,
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
                    **fx_fields,
                },
                correlation_id=uuid.uuid4().hex,
                occurred_at=now,
            )
        )
        return TransferOutcome(201, body)


def _insufficient(available_minor: int) -> TransferOutcome:
    return TransferOutcome(409, {"error": "insufficient_funds", "available_minor": available_minor})


def find_wallet(session: Session, user_id: uuid.UUID, currency: str) -> uuid.UUID | None:
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


def lock_balances(session: Session, account_ids: list[uuid.UUID]) -> dict[uuid.UUID, AccountBalance]:
    """`SELECT … FOR UPDATE` in ascending account id order so opposite transfers cannot deadlock."""
    rows = session.scalars(
        select(AccountBalance)
        .where(AccountBalance.account_id.in_(account_ids))
        .order_by(AccountBalance.account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {row.account_id: row for row in rows}


def _request_hash(email: str, amount_minor: int, currency: str, recipient_currency: str | None) -> str:
    canonical: dict[str, object] = {
        "recipient_email": email,
        "amount_minor": amount_minor,
        "currency": currency,
    }
    if recipient_currency is not None:
        canonical["recipient_currency"] = recipient_currency
    return _hash(canonical)


def _hash(canonical: dict[str, object]) -> str:
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
    # An allowlist: rows stored before web P-1 also carry the recipient's user id, hidden from the sender.
    body = {key: row.response_body[key] for key in _PUBLIC_BODY_KEYS if key in row.response_body}
    return TransferOutcome(201, body)


_PUBLIC_BODY_KEYS = (
    "transfer_id",
    "state",
    "amount_minor",
    "currency",
    "sender_available_minor",
    "recipient_currency",
    "recipient_amount_minor",
    "fx_rate_id",
)
