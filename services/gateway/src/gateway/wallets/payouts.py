import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass, field

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.payments.rails import RailPort, RailResult
from gateway.wallets.models import Payout
from gateway.wallets.service import HOLD_KIND, find_wallet, lock_balances

PAYOUT_METHODS = ("bank_transfer", "upi")
PAYOUT_CLEARING_KIND = "payout_clearing"
_FINAL_STATES = frozenset({"succeeded", "failed"})
_DEFAULT_FAILURE_CODE = "rail_failed"


@dataclass(frozen=True)
class PayoutDeps:
    session_factory: sessionmaker[Session]
    clock: Clock
    rail: RailPort
    request_hash_key: str | None = field(default=None, repr=False)
    """HMAC key for the stored request hash, so the raw destination cannot be brute-forced from it."""


class PayoutView(BaseModel):
    payout_id: uuid.UUID
    state: str
    method: str
    amount_minor: int
    currency: str
    destination_masked: str
    failure_code: str | None


@dataclass(frozen=True)
class PayoutOutcome:
    status_code: int
    """201 succeeded / failed, 202 pending / unknown, 409 insufficient_funds, 422 idempotency_key_reused."""
    body: dict[str, object]


class PayoutNotFoundError(Exception):
    pass


PAYOUT_KEY_REUSED = PayoutOutcome(422, {"error": "idempotency_key_reused"})


def create_payout(
    deps: PayoutDeps,
    user_id: uuid.UUID,
    idempotency_key: str,
    method: str,
    amount_minor: int,
    currency: str,
    destination: str,
) -> PayoutOutcome:
    """Hold in tx1, call the rail outside any transaction, then capture / release / keep the hold in tx2.

    Same key + same request → the payout's current view (no second hold, no second rail call).
    """
    if amount_minor <= 0:
        raise ValueError("amount_minor must be positive")
    if method not in PAYOUT_METHODS:
        raise ValueError(f"unsupported payout method {method!r}")
    request_hash = _request_hash(method, amount_minor, currency, destination, deps.request_hash_key)
    try:
        held = _hold(
            deps, user_id, idempotency_key, method, amount_minor, currency, destination, request_hash
        )
    except _KeyTakenConcurrentlyError:
        with deps.session_factory() as session:
            replay = _replay(session, user_id, idempotency_key, request_hash)
        return PAYOUT_KEY_REUSED if replay is None else replay
    if isinstance(held, PayoutOutcome):
        return held
    result = _submit(deps.rail, held, method, amount_minor, currency, destination)
    return _outcome(settle_payout(deps.session_factory, deps.clock, held, result))


def settle_payout(
    session_factory: sessionmaker[Session], clock: Clock, payout_id: uuid.UUID, result: RailResult
) -> PayoutView:
    """tx2 (also the reconciler / rail-callback entry point). A payout already succeeded or failed is
    never changed again; `unknown` keeps the hold. Locks the payout row, then the balance row."""
    with session_factory.begin() as session:
        payout = session.scalars(
            select(Payout)
            .where(Payout.id == payout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one_or_none()
        if payout is None:
            raise PayoutNotFoundError(str(payout_id))
        if payout.state in _FINAL_STATES:
            return _view(payout)
        now = clock()
        payout.updated_at = now
        if result.status == "unknown":
            payout.state = "unknown"
            return _view(payout)
        balance = lock_balances(session, [payout.account_id])[payout.account_id]
        hold_id = ensure_account(session, "user", payout.user_id, HOLD_KIND, payout.currency)
        amount, currency = payout.amount_minor, payout.currency
        balance.held_minor -= amount
        if result.status == "succeeded":
            clearing = ensure_account(session, "platform", None, PAYOUT_CLEARING_KIND, currency)
            lines = [PostingLine(hold_id, currency, amount), PostingLine(clearing, currency, -amount)]
            post_journal(session, clock, "payout_capture", payout.id, lines)
            payout.state = "succeeded"
        else:
            balance.available_minor += amount
            lines = [
                PostingLine(hold_id, currency, amount),
                PostingLine(payout.account_id, currency, -amount),
            ]
            post_journal(session, clock, "payout_release", payout.id, lines)
            payout.state = "failed"
            payout.failure_code = result.failure_code or _DEFAULT_FAILURE_CODE
        return _view(payout)


def get_payout(session: Session, user_id: uuid.UUID, payout_id: uuid.UUID) -> PayoutView:
    payout = session.scalars(
        select(Payout).where(Payout.id == payout_id, Payout.user_id == user_id)
    ).one_or_none()
    if payout is None:
        raise PayoutNotFoundError(str(payout_id))
    return _view(payout)


class _KeyTakenConcurrentlyError(Exception):
    pass


def _hold(
    deps: PayoutDeps,
    user_id: uuid.UUID,
    idempotency_key: str,
    method: str,
    amount_minor: int,
    currency: str,
    destination: str,
    request_hash: str,
) -> uuid.UUID | PayoutOutcome:
    """tx1: the payout id once funds are held, or the answer when nothing is to be sent to the rail."""
    with deps.session_factory.begin() as session:
        replay = _replay(session, user_id, idempotency_key, request_hash)
        if replay is not None:
            return replay
        wallet_id = find_wallet(session, user_id, currency)
        if wallet_id is None:
            return _insufficient(0)
        balance = lock_balances(session, [wallet_id])[wallet_id]
        # A same-key request that committed while this one waited for the lock answers with its payout.
        replay = _replay(session, user_id, idempotency_key, request_hash)
        if replay is not None:
            return replay
        if balance.available_minor < amount_minor:
            return _insufficient(balance.available_minor)
        balance.available_minor -= amount_minor
        balance.held_minor += amount_minor
        now = deps.clock()
        payout_id = uuid.uuid4()
        masked = mask_destination(destination)
        inserted = session.execute(
            insert(Payout)
            .values(
                id=payout_id,
                user_id=user_id,
                account_id=wallet_id,
                method=method,
                amount_minor=amount_minor,
                currency=currency,
                destination_masked=masked,
                state="pending",
                failure_code=None,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing()
            .returning(Payout.id)
        ).scalar_one_or_none()
        if inserted is None:
            raise _KeyTakenConcurrentlyError
        hold_id = ensure_account(session, "user", user_id, HOLD_KIND, currency)
        post_journal(
            session,
            deps.clock,
            "payout_hold",
            payout_id,
            [PostingLine(wallet_id, currency, amount_minor), PostingLine(hold_id, currency, -amount_minor)],
        )
        session.add(
            AuditLog(
                actor_id=f"user:{user_id}",
                actor_role="consumer",
                action="payout.created",
                target=f"payout:{payout_id}",
                summary={
                    "method": method,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "destination_masked": masked,
                },
                correlation_id=uuid.uuid4().hex,
                occurred_at=now,
            )
        )
        return payout_id


def _submit(
    rail: RailPort, payout_id: uuid.UUID, method: str, amount_minor: int, currency: str, destination: str
) -> RailResult:
    """Outside any transaction; the payout id is the rail's idempotency key. Transport failure = unknown."""
    try:
        return rail.submit(
            payout_id=str(payout_id),
            method=method,
            amount_minor=amount_minor,
            currency=currency,
            destination=destination,
        )
    except (TimeoutError, ConnectionError):
        return RailResult("unknown")


def mask_destination(destination: str) -> str:
    return "****" + destination[-4:]


def _replay(
    session: Session, user_id: uuid.UUID, idempotency_key: str, request_hash: str
) -> PayoutOutcome | None:
    payout = session.scalars(
        select(Payout)
        .where(Payout.user_id == user_id, Payout.idempotency_key == idempotency_key)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if payout is None:
        return None
    if payout.request_hash != request_hash:
        return PAYOUT_KEY_REUSED
    return _outcome(_view(payout))


def _outcome(view: PayoutView) -> PayoutOutcome:
    status_code = 201 if view.state in _FINAL_STATES else 202
    return PayoutOutcome(status_code, view.model_dump(mode="json"))


def _insufficient(available_minor: int) -> PayoutOutcome:
    return PayoutOutcome(409, {"error": "insufficient_funds", "available_minor": available_minor})


def _view(payout: Payout) -> PayoutView:
    return PayoutView(
        payout_id=payout.id,
        state=payout.state,
        method=payout.method,
        amount_minor=payout.amount_minor,
        currency=payout.currency,
        destination_masked=payout.destination_masked,
        failure_code=payout.failure_code,
    )


def _request_hash(method: str, amount_minor: int, currency: str, destination: str, key: str | None) -> str:
    canonical = {
        "method": method,
        "amount_minor": amount_minor,
        "currency": currency,
        "destination": destination,
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    if key is None:
        return hashlib.sha256(payload).hexdigest()
    return hmac.new(key.encode(), payload, hashlib.sha256).hexdigest()
