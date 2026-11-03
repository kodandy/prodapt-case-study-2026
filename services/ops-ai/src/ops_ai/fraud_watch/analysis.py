import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.fraud_watch import rules
from ops_ai.fraud_watch.rules import (
    EPISODE_GAP_SECONDS,
    HIGH_DECLINE_RATIO,
    MIN_DISTINCT_CARDS,
    MIN_MICRO_PAYMENTS,
    MIN_MICRO_SHARE,
    micro_condition,
)
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.tools import CARD_METHOD, FAILED_STATES, card_payments_in_window
from ops_ai.scope import Scope


@dataclass(frozen=True)
class CardTestingEpisode:
    first_payment_id: uuid.UUID
    start: datetime
    end: datetime
    payment_ids: tuple[uuid.UUID, ...]
    """The micro payments, ordered by (occurred_at, payment_id)."""
    micro_payments: int
    card_payments: int
    micro_share: float
    distinct_cards: int | None
    """None when any micro payment has no card_ref."""
    declined: int
    decline_ratio: float
    card_bins: tuple[str, ...]
    currencies: tuple[str, ...]
    flagged: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FraudAnalysis:
    start: datetime
    end: datetime
    micro_payments: int
    episodes: tuple[CardTestingEpisode, ...]
    """Newest start first; episodes under MIN_MICRO_PAYMENTS are left out."""
    truncated: bool = False
    """True when the window holds more than MAX_ANALYZED_MICRO_PAYMENTS micro payments; only the newest
    ones were analysed, while `micro_payments` counts them all."""


@dataclass(frozen=True)
class _Payment:
    payment_id: uuid.UUID
    occurred_at: datetime
    amount_minor: int
    currency: str
    state: str
    card_bin: str | None
    card_ref: str | None


def analyze_fraud(session: Session, scope: Scope, start: datetime, end: datetime) -> FraudAnalysis:
    """Splits the merchant's micro card payments in [start, end) into episodes by the gap rule. Read-only.

    Loads at most MAX_ANALYZED_MICRO_PAYMENTS micro payments, the newest by (occurred_at, payment_id).
    """
    limit = rules.MAX_ANALYZED_MICRO_PAYMENTS
    window = [*card_payments_in_window(scope, start, end), micro_condition()]
    micro_count = session.execute(select(func.count()).select_from(PaymentFact).where(*window)).scalar_one()
    query = (
        select(
            PaymentFact.payment_id,
            PaymentFact.occurred_at,
            PaymentFact.amount_minor,
            PaymentFact.currency,
            PaymentFact.state,
            PaymentFact.card_bin,
            PaymentFact.card_ref,
        )
        .where(*window)
        .order_by(PaymentFact.occurred_at.desc(), PaymentFact.payment_id.desc())
        .limit(limit)
    )
    micro = [
        _Payment(
            payment_id=row.payment_id,
            occurred_at=row.occurred_at,
            amount_minor=row.amount_minor,
            currency=row.currency,
            state=row.state,
            card_bin=row.card_bin,
            card_ref=row.card_ref,
        )
        for row in session.execute(query)
    ]
    micro.reverse()
    episodes = [
        _episode(group, _card_payments_in_span(session, scope, group))
        for group in _split_by_gap(micro)
        if len(group) >= MIN_MICRO_PAYMENTS
    ]
    episodes.sort(key=lambda episode: episode.start, reverse=True)
    return FraudAnalysis(
        start=start,
        end=end,
        micro_payments=micro_count,
        episodes=tuple(episodes),
        truncated=micro_count > limit,
    )


def _card_payments_in_span(session: Session, scope: Scope, micro: list[_Payment]) -> int:
    """Card payments of any amount in [first micro, last micro], both ends included."""
    return session.execute(
        select(func.count())
        .select_from(PaymentFact)
        .where(
            PaymentFact.merchant_id == scope.merchant_id,
            PaymentFact.method == CARD_METHOD,
            PaymentFact.occurred_at >= micro[0].occurred_at,
            PaymentFact.occurred_at <= micro[-1].occurred_at,
        )
    ).scalar_one()


def _split_by_gap(micro: list[_Payment]) -> list[list[_Payment]]:
    gap = timedelta(seconds=EPISODE_GAP_SECONDS)
    groups: list[list[_Payment]] = []
    for row in micro:
        if groups and row.occurred_at - groups[-1][-1].occurred_at <= gap:
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _episode(micro: list[_Payment], card_payments: int) -> CardTestingEpisode:
    start, end = micro[0].occurred_at, micro[-1].occurred_at
    card_refs = [row.card_ref for row in micro]
    distinct_cards = None if None in card_refs else len(set(card_refs))
    declined = sum(1 for row in micro if row.state in FAILED_STATES)
    micro_share = len(micro) / card_payments
    decline_ratio = declined / len(micro)
    flagged = (
        len(micro) >= MIN_MICRO_PAYMENTS
        and micro_share >= MIN_MICRO_SHARE
        and (distinct_cards is None or distinct_cards >= MIN_DISTINCT_CARDS)
    )
    return CardTestingEpisode(
        first_payment_id=micro[0].payment_id,
        start=start,
        end=end,
        payment_ids=tuple(row.payment_id for row in micro),
        micro_payments=len(micro),
        card_payments=card_payments,
        micro_share=micro_share,
        distinct_cards=distinct_cards,
        declined=declined,
        decline_ratio=decline_ratio,
        card_bins=tuple(sorted({row.card_bin for row in micro if row.card_bin is not None})),
        currencies=tuple(sorted({row.currency for row in micro})),
        flagged=flagged,
        reasons=_reasons(distinct_cards, decline_ratio) if flagged else (),
    )


def _reasons(distinct_cards: int | None, decline_ratio: float) -> tuple[str, ...]:
    reasons = ["micro_burst"]
    if distinct_cards is not None and distinct_cards >= MIN_DISTINCT_CARDS:
        reasons.append("many_cards")
    if distinct_cards is None:
        reasons.append("card_identity_unknown")
    if decline_ratio >= HIGH_DECLINE_RATIO:
        reasons.append("high_decline_ratio")
    return tuple(reasons)
