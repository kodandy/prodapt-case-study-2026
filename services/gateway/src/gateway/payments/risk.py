import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from gateway.payments.disputes import PaymentNotFoundError
from gateway.payments.models import Payment
from gateway.payments.schemas import PaymentRiskView

HIGH_AMOUNT_MINOR = {"USD": 500_000, "EUR": 500_000, "JPY": 500_000, "INR": 40_000_000}
VELOCITY_LIMIT = 3
VELOCITY_WINDOW = timedelta(minutes=10)
DECLINE_LIMIT = 2
DECLINE_WINDOW = timedelta(minutes=60)
FLAG_THRESHOLD = Decimal("0.50")
BLOCK_THRESHOLD = Decimal("0.90")
MAX_SCORE = Decimal("1.00")
RISK_BLOCKED_DECLINE_CODE = "risk_blocked"

_HIGH_AMOUNT_WEIGHT = Decimal("0.40")
_VELOCITY_WEIGHT = Decimal("0.40")
_DECLINES_WEIGHT = Decimal("0.30")


@dataclass(frozen=True)
class RiskAssessment:
    score: Decimal
    reasons: tuple[str, ...]
    flagged: bool
    blocked: bool


def assess_charge_risk(
    session: Session, now: datetime, card_fingerprint: str, amount_minor: int, currency: str
) -> RiskAssessment:
    """Rule-based score for a charge about to be created; velocity counts earlier payments on the card."""
    score = Decimal("0.00")
    reasons: list[str] = []
    if amount_minor >= HIGH_AMOUNT_MINOR[currency]:
        score += _HIGH_AMOUNT_WEIGHT
        reasons.append("high_amount")
    recent, declines = _card_activity(session, now, card_fingerprint)
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


def _card_activity(session: Session, now: datetime, card_fingerprint: str) -> tuple[int, int]:
    """(payments in the velocity window, non-risk-blocked declines in the decline window). The per-card
    advisory lock serialises concurrent charges on one card until the caller's transaction ends."""
    session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(card_fingerprint, 0))))
    row = session.execute(
        select(
            func.count().filter(Payment.created_at >= now - VELOCITY_WINDOW),
            func.count().filter(Payment.state == "declined", Payment.risk_blocked.is_(False)),
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
