import hashlib
import json
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.audit.models import AuditLog
from gateway.clock import Clock
from gateway.outbox.models import OutboxEvent
from gateway.payments.idempotency.models import IdempotencyRecord
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from gateway.payments.models import Payment, PaymentAttempt
from gateway.payments.providers.port import CardRef, ProviderPort, ProviderResult, ProviderTimeoutError
from gateway.payments.schemas import CardSummary, ChargeRequest, ChargeResponse
from gateway.payments.state import FINALIZABLE_STATES, PaymentState
from gateway.payments.tokenization.cards import card_fingerprint, derive_card_ref
from gateway.payments.tokenization.vault import tokenize_card

IDEMPOTENCY_WAIT_SECONDS = 5.0
ATTEMPT_LEASE_SECONDS = 30
REDIS_LOCK_MILLISECONDS = 10_000
POLL_INTERVAL_SECONDS = 0.02
ROUTE_CONFIG_VERSION = 1

_RELEASE_LOCK_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end return 0"
)


@dataclass(frozen=True)
class ChargeHooks:
    """Test-only fault injection points (crash matrix)."""

    after_tx2_commit: Callable[[], None] | None = None


@dataclass(frozen=True)
class ChargeDeps:
    session_factory: sessionmaker[Session]
    redis: Redis | None
    """None models Redis being down; correctness must not depend on it."""
    provider: ProviderPort
    clock: Clock
    card_fingerprint_key: str
    hooks: ChargeHooks = field(default_factory=ChargeHooks)
    idempotency_wait_seconds: float = IDEMPOTENCY_WAIT_SECONDS


@dataclass(frozen=True)
class ChargeOutcome:
    status_code: int
    """201 completed (succeeded or declined), 409 idempotency_in_progress, 422 idempotency_key_reused."""
    body: dict[str, Any]


@dataclass(frozen=True)
class _Claim:
    payment_id: uuid.UUID
    attempt_id: uuid.UUID
    card: CardRef
    card_fingerprint: str


KEY_REUSED = ChargeOutcome(422, {"error": "idempotency_key_reused"})
IN_PROGRESS = ChargeOutcome(409, {"error": "idempotency_in_progress"})


def charge_card(
    deps: ChargeDeps, merchant_id: uuid.UUID, idempotency_key: str, request: ChargeRequest
) -> ChargeOutcome:
    """Sync card charge (architecture-detail §3 "Sync charge path").

    - Same key + same request: one provider charge; every caller gets the same stored body.
    - Same key + different request: 422 `idempotency_key_reused`.
    - Same key still in flight after the wait budget: 409 `idempotency_in_progress`.
    - On success, tx2 writes the journal (balanced), the outbox event (gw.payments.v1), the audit row and
      the stored response in one transaction.
    """
    pan = request.card.number.get_secret_value()
    request_hash = _request_hash(request, card_fingerprint(pan, deps.card_fingerprint_key))
    lock_key = f"idem:{merchant_id}:{idempotency_key}"
    lock_token = _try_lock(deps.redis, lock_key)
    if lock_token is False:
        return _await_stored(deps, merchant_id, idempotency_key, request_hash)
    try:
        claim = _claim(deps, merchant_id, idempotency_key, request_hash, request, pan)
        if claim is None:
            return _await_stored(deps, merchant_id, idempotency_key, request_hash)
        result = _call_provider(deps.provider, claim, request)
        outcome = _finalize(deps, merchant_id, idempotency_key, claim, request, result)
    finally:
        if isinstance(lock_token, str):
            _release_lock(deps.redis, lock_key, lock_token)
    if outcome is None:
        return _await_stored(deps, merchant_id, idempotency_key, request_hash)
    if deps.hooks.after_tx2_commit is not None:
        deps.hooks.after_tx2_commit()
    return outcome


def _request_hash(request: ChargeRequest, fingerprint: str) -> str:
    canonical = {
        "amount_minor": request.amount_minor,
        "currency": request.currency,
        "method": request.method,
        "card": {
            "fingerprint": fingerprint,
            "exp_month": request.card.exp_month,
            "exp_year": request.card.exp_year,
        },
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _try_lock(redis: Redis | None, key: str) -> str | bool | None:
    """Token if acquired, False if someone else holds it, None if Redis is unavailable."""
    if redis is None:
        return None
    token = secrets.token_hex(16)
    try:
        acquired = redis.set(key, token, nx=True, px=REDIS_LOCK_MILLISECONDS)
    except RedisError:
        return None
    return token if acquired else False


def _release_lock(redis: Redis | None, key: str, token: str) -> None:
    if redis is None:
        return
    try:
        redis.eval(_RELEASE_LOCK_SCRIPT, 1, key, token)
    except RedisError:
        return


def _claim(
    deps: ChargeDeps,
    merchant_id: uuid.UUID,
    idempotency_key: str,
    request_hash: str,
    request: ChargeRequest,
    pan: str,
) -> _Claim | None:
    """tx1: idempotency row + token + payment + attempt. None if the key is already taken."""
    with deps.session_factory.begin() as session:
        now = deps.clock()
        inserted = session.execute(
            insert(IdempotencyRecord)
            .values(
                merchant_id=merchant_id,
                key=idempotency_key,
                request_hash=request_hash,
                lease_until=now + timedelta(seconds=ATTEMPT_LEASE_SECONDS),
                created_at=now,
            )
            .on_conflict_do_nothing()
            .returning(IdempotencyRecord.key)
        ).scalar_one_or_none()
        if inserted is None:
            return None
        card = tokenize_card(
            session, deps.clock, pan, request.card.exp_month, request.card.exp_year, deps.card_fingerprint_key
        )
        payment = Payment(
            id=uuid.uuid4(),
            merchant_id=merchant_id,
            amount_minor=request.amount_minor,
            currency=request.currency,
            method=request.method,
            state="created",
            card_token=card.token,
            entity_version=1,
            created_at=now,
            updated_at=now,
        )
        session.add(payment)
        session.flush()
        attempt = PaymentAttempt(
            id=uuid.uuid4(),
            payment_id=payment.id,
            provider=deps.provider.name,
            route_config_version=ROUTE_CONFIG_VERSION,
            state="created",
            lease_until=now + timedelta(seconds=ATTEMPT_LEASE_SECONDS),
            created_at=now,
            updated_at=now,
        )
        session.add(attempt)
        session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.merchant_id == merchant_id, IdempotencyRecord.key == idempotency_key)
            .values(payment_id=payment.id)
        )
        return _Claim(
            payment_id=payment.id,
            attempt_id=attempt.id,
            card=CardRef(token=card.token, bin=card.bin, last4=card.last4),
            card_fingerprint=card.fingerprint,
        )


def _call_provider(provider: ProviderPort, claim: _Claim, request: ChargeRequest) -> ProviderResult | None:
    """Outside any transaction. None means the outcome is unknown (timeout or transport failure)."""
    try:
        return provider.charge(str(claim.attempt_id), request.amount_minor, request.currency, claim.card)
    except (ProviderTimeoutError, ConnectionError, TimeoutError):
        return None


def _finalize(
    deps: ChargeDeps,
    merchant_id: uuid.UUID,
    idempotency_key: str,
    claim: _Claim,
    request: ChargeRequest,
    result: ProviderResult | None,
) -> ChargeOutcome | None:
    """tx2. None if another writer (the reconciler) finalized first: the caller reads the stored response."""
    state: PaymentState = "unknown" if result is None else result.status
    decline_code = None if result is None else result.decline_code
    with deps.session_factory.begin() as session:
        now = deps.clock()
        entity_version = session.execute(
            update(Payment)
            .where(Payment.id == claim.payment_id, Payment.state.in_(FINALIZABLE_STATES))
            .values(
                state=state,
                decline_code=decline_code,
                updated_at=now,
                entity_version=Payment.entity_version + 1,
            )
            .returning(Payment.entity_version)
        ).scalar_one_or_none()
        if entity_version is None:
            return None
        session.execute(
            update(PaymentAttempt)
            .where(PaymentAttempt.id == claim.attempt_id)
            .values(
                state=state,
                provider_ref=None if result is None else result.provider_ref,
                decline_code=decline_code,
                updated_at=now,
            )
        )
        response = ChargeResponse(
            payment_id=claim.payment_id,
            state=state,
            amount_minor=request.amount_minor,
            currency=request.currency,
            method=request.method,
            card=CardSummary(bin=claim.card.bin, last4=claim.card.last4),
            decline_code=decline_code,
        ).model_dump(mode="json")
        if state == "unknown":
            session.execute(
                update(IdempotencyRecord)
                .where(IdempotencyRecord.merchant_id == merchant_id, IdempotencyRecord.key == idempotency_key)
                .values(response_status=202, response_body=response)
            )
            return ChargeOutcome(202, response)
        if state == "succeeded":
            _post_charge_journal(session, deps.clock, merchant_id, claim.payment_id, request)
        _write_outbox(
            session,
            now,
            merchant_id,
            claim,
            request,
            state,
            entity_version,
            deps.provider.name,
            decline_code,
            derive_card_ref(claim.card_fingerprint, merchant_id, deps.card_fingerprint_key),
        )
        session.add(
            AuditLog(
                actor_id=f"merchant:{merchant_id}",
                actor_role="merchant_api",
                action="payment.charge",
                target=f"payment:{claim.payment_id}",
                summary={"state": state, "amount_minor": request.amount_minor, "currency": request.currency},
                correlation_id=uuid.uuid4().hex,
                occurred_at=now,
            )
        )
        session.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.merchant_id == merchant_id, IdempotencyRecord.key == idempotency_key)
            .values(response_status=201, response_body=response)
        )
        return ChargeOutcome(201, response)


def _post_charge_journal(
    session: Session, clock: Clock, merchant_id: uuid.UUID, payment_id: uuid.UUID, request: ChargeRequest
) -> None:
    receivable = ensure_account(session, "platform", None, "provider_receivable", request.currency)
    payable = ensure_account(session, "merchant", merchant_id, "merchant_payable", request.currency)
    post_journal(
        session,
        clock,
        "payment",
        payment_id,
        [
            PostingLine(receivable, request.currency, request.amount_minor),
            PostingLine(payable, request.currency, -request.amount_minor),
        ],
    )


def _write_outbox(
    session: Session,
    now: datetime,
    merchant_id: uuid.UUID,
    claim: _Claim,
    request: ChargeRequest,
    state: PaymentState,
    entity_version: int,
    provider: str,
    decline_code: str | None,
    card_ref: str,
) -> None:
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.succeeded" if state == "succeeded" else "payment.declined",
        occurred_at=now,
        entity_version=entity_version,
        payment_id=claim.payment_id,
        merchant_id=merchant_id,
        amount_minor=request.amount_minor,
        currency=request.currency,
        method=request.method,
        state=state,
        provider=provider,
        decline_code=decline_code,
        card_bin=claim.card.bin,
        card_last4=claim.card.last4,
        card_ref=card_ref,
    )
    session.add(
        OutboxEvent(
            event_id=event.event_id,
            topic=PAYMENTS_TOPIC,
            payload=event.model_dump(mode="json"),
            headers={},
            created_at=now,
            published_at=None,
        )
    )


def _await_stored(
    deps: ChargeDeps, merchant_id: uuid.UUID, idempotency_key: str, request_hash: str
) -> ChargeOutcome:
    deadline = time.monotonic() + deps.idempotency_wait_seconds
    while True:
        with deps.session_factory() as session:
            record = session.get(IdempotencyRecord, (merchant_id, idempotency_key))
            if record is not None:
                if record.request_hash != request_hash:
                    return KEY_REUSED
                if record.response_status is not None and record.response_body is not None:
                    return ChargeOutcome(record.response_status, record.response_body)
        if time.monotonic() >= deadline:
            return IN_PROGRESS
        time.sleep(POLL_INTERVAL_SECONDS)
