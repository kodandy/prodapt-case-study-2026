import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from gateway.payments.disputes import PaymentNotFoundError
from gateway.payments.models import Payment
from gateway.payments.schemas import PaymentRiskView
from gateway.payments.state import ATTEMPT_LEASE_SECONDS

_log = logging.getLogger(__name__)

HIGH_AMOUNT_MINOR = {"USD": 500_000, "EUR": 500_000, "JPY": 500_000, "INR": 40_000_000}
VELOCITY_LIMIT = 3
VELOCITY_WINDOW = timedelta(minutes=10)
DECLINE_LIMIT = 2
DECLINE_WINDOW = timedelta(minutes=60)
IN_FLIGHT_WINDOW = timedelta(seconds=ATTEMPT_LEASE_SECONDS)
FLAG_THRESHOLD = Decimal("0.50")
BLOCK_THRESHOLD = Decimal("0.90")
MAX_SCORE = Decimal("1.00")
RISK_BLOCKED_DECLINE_CODE = "risk_blocked"
RISK_UNAVAILABLE_DECLINE_CODE = "risk_unavailable"
DEGRADED_REASON = "degraded_scoring"

_HIGH_AMOUNT_WEIGHT = Decimal("0.40")
_VELOCITY_WEIGHT = Decimal("0.40")
_DECLINES_WEIGHT = Decimal("0.30")


VelocityLookup = Callable[[Session, datetime, str], tuple[int, int]]


@dataclass(frozen=True)
class RiskAssessment:
    score: Decimal
    reasons: tuple[str, ...]
    flagged: bool
    blocked: bool
    degraded: bool = False
    decline_code: str = RISK_BLOCKED_DECLINE_CODE


def score_activity(amount_minor: int, currency: str, recent: int, declines: int) -> RiskAssessment:
    """The pure rule: amount, card velocity and recent declines, capped at MAX_SCORE."""
    score = Decimal("0.00")
    reasons: list[str] = []
    if amount_minor >= HIGH_AMOUNT_MINOR[currency]:
        score += _HIGH_AMOUNT_WEIGHT
        reasons.append("high_amount")
    if recent >= VELOCITY_LIMIT:
        score += _VELOCITY_WEIGHT
        reasons.append("card_velocity")
    if declines >= DECLINE_LIMIT:
        score += _DECLINES_WEIGHT
        reasons.append("recent_declines")
    score = min(score, MAX_SCORE)
    return RiskAssessment(
        score=score, reasons=tuple(reasons), flagged=score >= FLAG_THRESHOLD, blocked=score >= BLOCK_THRESHOLD
    )


def assess_charge_risk(
    session: Session,
    now: datetime,
    card_fingerprint: str,
    amount_minor: int,
    currency: str,
    velocity_lookup: VelocityLookup | None = None,
) -> RiskAssessment:
    """Rule-based score for a charge about to be created; velocity counts earlier payments on the card.

    B7.1: when the velocity lookup fails with a database error the charge is scored on the amount rule
    alone, always flagged, and blocked `risk_unavailable` if the amount is high (fail closed for high value).
    """
    lookup = velocity_lookup if velocity_lookup is not None else card_activity
    try:
        with session.begin_nested():
            recent, declines = lookup(session, now, card_fingerprint)
    except SQLAlchemyError as exc:
        _log.warning("risk velocity lookup failed; scoring degraded (%s)", type(exc).__name__)
        return _degraded(amount_minor, currency)
    return score_activity(amount_minor, currency, recent, declines)


def _degraded(amount_minor: int, currency: str) -> RiskAssessment:
    high = amount_minor >= HIGH_AMOUNT_MINOR[currency]
    reasons = ("high_amount", DEGRADED_REASON) if high else (DEGRADED_REASON,)
    return RiskAssessment(
        score=_HIGH_AMOUNT_WEIGHT if high else Decimal("0.00"),
        reasons=reasons,
        flagged=True,
        blocked=high,
        degraded=True,
        decline_code=RISK_UNAVAILABLE_DECLINE_CODE if high else RISK_BLOCKED_DECLINE_CODE,
    )


def card_activity(session: Session, now: datetime, card_fingerprint: str) -> tuple[int, int]:
    """(payments in the velocity window, non-risk-blocked declines in the decline window plus in-flight
    siblings). The per-card advisory lock serialises concurrent charges on one card until the caller's
    transaction ends, so a sibling still waiting on the provider is seen as `created` and counts as a
    decline."""
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(card_fingerprint, 0))))
    declined = and_(Payment.state == "declined", Payment.risk_blocked.is_(False))
    in_flight = and_(Payment.state == "created", Payment.created_at >= now - IN_FLIGHT_WINDOW)
    row = session.execute(
        select(
            func.count().filter(Payment.created_at >= now - VELOCITY_WINDOW),
            func.count().filter(or_(declined, in_flight)),
        ).where(Payment.card_fingerprint == card_fingerprint, Payment.created_at >= now - DECLINE_WINDOW)
    ).one()
    return row[0], row[1]


def get_payment_risk(session: Session, merchant_id: uuid.UUID, payment_id: uuid.UUID) -> PaymentRiskView:
    """Flags only: score and reasons draw on the card's activity at every merchant, so they stay internal."""
    payment = session.scalars(
        select(Payment).where(Payment.id == payment_id, Payment.merchant_id == merchant_id)
    ).one_or_none()
    if payment is None:
        raise PaymentNotFoundError
    return PaymentRiskView(
        payment_id=payment.id,
        risk_flagged=payment.risk_flagged,
        blocked=payment.risk_blocked,
    )
