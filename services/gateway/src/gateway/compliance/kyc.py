"""AML / KYC re-verification triggers (R2-10). Pure rules + a screening step that runs before money moves."""

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Literal

from contracts.compliance import COMPLIANCE_TOPIC, KycReviewRequiredV1, KycRuleName
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import User
from gateway.admin import OpsPrincipal
from gateway.audit.service import record_audit
from gateway.clock import Clock
from gateway.compliance.models import KycReview
from gateway.outbox.models import OutboxEvent
from gateway.wallets.models import Transfer

KycRule = KycRuleName
OperationKind = Literal["transfer", "payout"]
KycOutcome = Literal["verified", "rejected"]
ReviewStatus = Literal["open", "verified", "rejected"]

AMOUNT_THRESHOLD_MINOR = MappingProxyType(
    {"USD": 1_000_000, "EUR": 1_000_000, "INR": 80_000_000, "JPY": 1_500_000}
)
VOLUME_MULTIPLIER = 2
VOLUME_WINDOW = timedelta(hours=24)
HIGH_RISK_COUNTRIES = frozenset({"KP", "IR", "MM"})
KYC_MAX_AGE = timedelta(days=365)
VELOCITY_MAX_TRANSFERS = 10
VELOCITY_WINDOW = timedelta(hours=1)

KYC_REVIEW_OPENED_ACTION = "kyc.review_opened"
OPERATION_BLOCKED_ACTION = "kyc.operation_blocked"
REVERIFIED_ACTION = "kyc.reverified"
REJECTED_ACTION = "kyc.rejected"

_SUCCEEDED_TRANSFER = "succeeded"


@dataclass(frozen=True, kw_only=True)
class KycFacts:
    amount_minor: int
    currency: str
    volume_24h_minor: int
    transfers_last_hour: int
    sender_country: str | None
    recipient_country: str | None
    kyc_status: str
    kyc_verified_at: datetime | None
    kyc_expires_at: datetime | None


@dataclass(frozen=True, kw_only=True)
class OperationRequest:
    kind: OperationKind
    amount_minor: int
    currency: str
    recipient_email: str | None = None


@dataclass(frozen=True)
class ScreeningResult:
    triggered: tuple[KycRule, ...]
    cross_border: bool
    high_risk: bool
    blocked: bool
    kyc_status: str


@dataclass(frozen=True)
class KycResolution:
    user_id: uuid.UUID
    kyc_status: str
    kyc_expires_at: datetime | None


@dataclass(frozen=True)
class KycReviewView:
    review_id: uuid.UUID
    user_id: uuid.UUID
    status: str
    reasons: tuple[str, ...]
    opened_at: datetime
    resolved_at: datetime | None
    resolved_by: str | None


class UnknownUserError(Exception):
    pass


def amount_threshold(currency: str) -> int:
    """Raises ValueError for a currency without a threshold, so an unlisted currency is never unscreened."""
    threshold = AMOUNT_THRESHOLD_MINOR.get(currency)
    if threshold is None:
        raise ValueError("no AML amount threshold for currency")
    return threshold


def evaluate_kyc_rules(facts: KycFacts, now: datetime) -> tuple[KycRule, ...]:
    """The fact-based rules that fire, in the order of the spec table (`name_change` is not fact-based)."""
    threshold = amount_threshold(facts.currency)
    fired: list[KycRule] = []
    if facts.amount_minor >= threshold:
        fired.append("amount_threshold")
    if facts.volume_24h_minor + facts.amount_minor >= VOLUME_MULTIPLIER * threshold:
        fired.append("volume_24h")
    if _high_risk_pair(facts.sender_country, facts.recipient_country):
        fired.append("high_risk_country_pair")
    if facts.kyc_status == "verified":
        if facts.kyc_expires_at is not None and facts.kyc_expires_at <= now:
            fired.append("kyc_expired")
        if facts.kyc_verified_at is not None and facts.kyc_verified_at + KYC_MAX_AGE <= now:
            fired.append("kyc_stale")
    if facts.transfers_last_hour >= VELOCITY_MAX_TRANSFERS:
        fired.append("velocity")
    return tuple(fired)


def screen_operation(
    session_factory: sessionmaker[Session], clock: Clock, user_id: uuid.UUID, request: OperationRequest
) -> ScreeningResult:
    """Commits its own transaction (user row locked). Fired rules move the user to `pending` and open
    or extend the user's single open review; a high-risk operation by a user who is not `verified` is
    blocked. Callers that act on the result must hold `user_operation_lock` across screening and the
    operation, or concurrent operations are screened against the same stale history."""
    with session_factory.begin() as session:
        user = session.get(User, user_id, with_for_update=True)
        if user is None:
            raise UnknownUserError
        now = clock()
        recipient_country = _recipient_country(session, request)
        facts = KycFacts(
            amount_minor=request.amount_minor,
            currency=request.currency,
            volume_24h_minor=_sent_volume(session, user_id, request.currency, now - VOLUME_WINDOW),
            transfers_last_hour=_sent_count(session, user_id, now - VELOCITY_WINDOW),
            sender_country=user.country,
            recipient_country=recipient_country,
            kyc_status=user.kyc_status,
            kyc_verified_at=user.kyc_verified_at,
            kyc_expires_at=user.kyc_expires_at,
        )
        triggered = evaluate_kyc_rules(facts, now)
        if triggered:
            flag_for_review(session, clock, user, triggered, request.kind)
        cross_border = (
            request.kind == "transfer"
            and user.country is not None
            and recipient_country is not None
            and user.country != recipient_country
        )
        large_payout = request.kind == "payout" and request.amount_minor >= amount_threshold(request.currency)
        high_risk = cross_border or large_payout
        blocked = high_risk and user.kyc_status != "verified"
        if blocked:
            actor = f"user:{user.id}"
            record_audit(
                session,
                clock,
                actor,
                "consumer",
                OPERATION_BLOCKED_ACTION,
                actor,
                {
                    "operation": request.kind,
                    "cross_border": cross_border,
                    "large_payout": large_payout,
                    "kyc_status": user.kyc_status,
                },
            )
        return ScreeningResult(
            triggered=triggered,
            cross_border=cross_border,
            high_risk=high_risk,
            blocked=blocked,
            kyc_status=user.kyc_status,
        )


class OperationInProgressError(Exception):
    """Another screened operation of the same user is still running."""


@contextmanager
def user_operation_lock(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> Iterator[None]:
    """Transaction-scoped advisory lock on the user, held until the block exits. Try-lock rather than
    wait: a waiting request would pin a pooled connection while the holder needs a second one."""
    with session_factory.begin() as session:
        acquired = session.execute(
            select(func.pg_try_advisory_xact_lock(func.hashtextextended(f"kyc-operation:{user_id}", 0)))
        ).scalar_one()
        if not acquired:
            raise OperationInProgressError
        yield


def flag_for_review(
    session: Session, clock: Clock, user: User, rules: tuple[KycRule, ...], operation: str
) -> KycReview:
    """In the caller's transaction: `pending` (a `rejected` user stays rejected), one open review holding the
    union of rules, an audit row and a `compliance.kyc_review_required` outbox event."""
    now = clock()
    if user.kyc_status != "rejected":
        user.kyc_status = "pending"
    review = session.scalars(
        select(KycReview).where(KycReview.user_id == user.id, KycReview.status == "open").with_for_update()
    ).one_or_none()
    if review is None:
        review = KycReview(
            id=uuid.uuid4(),
            user_id=user.id,
            status="open",
            reasons=list(rules),
            opened_at=now,
            resolved_at=None,
            resolved_by=None,
        )
        session.add(review)
    else:
        review.reasons = [*review.reasons, *(rule for rule in rules if rule not in review.reasons)]
    session.flush()
    event = KycReviewRequiredV1(
        event_id=uuid.uuid4(),
        event_type="compliance.kyc_review_required",
        occurred_at=now,
        review_id=review.id,
        user_id=user.id,
        rules=tuple(_as_rule(reason) for reason in review.reasons),
    )
    session.add(
        OutboxEvent(
            event_id=event.event_id,
            topic=COMPLIANCE_TOPIC,
            payload=event.model_dump(mode="json"),
            headers={},
            created_at=now,
            published_at=None,
        )
    )
    actor = f"user:{user.id}"
    record_audit(
        session,
        clock,
        actor,
        "consumer",
        KYC_REVIEW_OPENED_ACTION,
        actor,
        {"review_id": str(review.id), "rules": list(rules), "operation": operation},
    )
    return review


def resolve_kyc(
    session_factory: sessionmaker[Session],
    clock: Clock,
    principal: OpsPrincipal,
    user_id: uuid.UUID,
    outcome: KycOutcome,
) -> KycResolution:
    """Simulated ops re-verification. Commits; raises UnknownUserError (nothing written) for an
    unknown or erased user."""
    if outcome not in ("verified", "rejected"):
        raise ValueError("unknown kyc outcome")
    actor = f"ops:{principal.ops_user_id}"
    with session_factory.begin() as session:
        user = session.get(User, user_id, with_for_update=True)
        if user is None or user.erased_at is not None:
            raise UnknownUserError
        now = clock()
        user.kyc_status = outcome
        if outcome == "verified":
            user.kyc_verified_at = now
            user.kyc_expires_at = now + KYC_MAX_AGE
        open_reviews = session.scalars(
            select(KycReview)
            .where(KycReview.user_id == user_id, KycReview.status == "open")
            .with_for_update()
        ).all()
        for review in open_reviews:
            review.status = outcome
            review.resolved_at = now
            review.resolved_by = actor
        record_audit(
            session,
            clock,
            actor,
            principal.role,
            REVERIFIED_ACTION if outcome == "verified" else REJECTED_ACTION,
            f"user:{user_id}",
            {"outcome": outcome, "reviews_closed": len(open_reviews)},
        )
        return KycResolution(user_id=user.id, kyc_status=user.kyc_status, kyc_expires_at=user.kyc_expires_at)


def list_kyc_reviews(session: Session, status: ReviewStatus | None, limit: int) -> list[KycReviewView]:
    """Newest first."""
    query = select(KycReview).order_by(KycReview.opened_at.desc(), KycReview.id.desc()).limit(limit)
    if status is not None:
        query = query.where(KycReview.status == status)
    return [
        KycReviewView(
            review_id=review.id,
            user_id=review.user_id,
            status=review.status,
            reasons=tuple(review.reasons),
            opened_at=review.opened_at,
            resolved_at=review.resolved_at,
            resolved_by=review.resolved_by,
        )
        for review in session.scalars(query)
    ]


def _high_risk_pair(sender: str | None, recipient: str | None) -> bool:
    if sender is None or recipient is None or sender == recipient:
        return False
    return sender in HIGH_RISK_COUNTRIES or recipient in HIGH_RISK_COUNTRIES


def _recipient_country(session: Session, request: OperationRequest) -> str | None:
    if request.kind != "transfer" or request.recipient_email is None:
        return None
    email = request.recipient_email.strip().lower()
    return session.scalar(select(User.country).where(User.email == email))


def _sent_volume(session: Session, user_id: uuid.UUID, currency: str, since: datetime) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(Transfer.amount_minor), 0)).where(
            Transfer.sender_user_id == user_id,
            Transfer.currency == currency,
            Transfer.state == _SUCCEEDED_TRANSFER,
            Transfer.created_at > since,
        )
    )
    return int(total or 0)


def _sent_count(session: Session, user_id: uuid.UUID, since: datetime) -> int:
    count = session.scalar(
        select(func.count())
        .select_from(Transfer)
        .where(
            Transfer.sender_user_id == user_id,
            Transfer.state == _SUCCEEDED_TRANSFER,
            Transfer.created_at > since,
        )
    )
    return int(count or 0)


def _as_rule(reason: str) -> KycRule:
    for rule in _ALL_RULES:
        if rule == reason:
            return rule
    raise ValueError("unknown kyc rule stored on review")


_ALL_RULES: tuple[KycRule, ...] = (
    "amount_threshold",
    "volume_24h",
    "high_risk_country_pair",
    "kyc_expired",
    "kyc_stale",
    "velocity",
    "name_change",
)
