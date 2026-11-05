"""Per-merchant, versioned card-testing thresholds driven by ops feedback (R2-7).

Every change is a new append-only row, so the history is the audit trail and any version can be restored.
The ladder only tightens: no step is looser than the shipped baseline in `fraud_watch.rules`.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.clock import Clock
from ops_ai.feedback.models import FraudRuleVersion
from ops_ai.fraud_watch.analysis import CardTestingEpisode
from ops_ai.fraud_watch.rules import MIN_DISTINCT_CARDS, MIN_MICRO_PAYMENTS, MIN_MICRO_SHARE
from ops_ai.scope import Scope

MAX_STEPS = 5
MAX_MICRO_SHARE = 0.95


@dataclass(frozen=True)
class RuleVersion:
    version: int
    steps: int
    min_micro_payments: int
    min_micro_share: float
    min_distinct_cards: int
    reason: str
    feedback_id: uuid.UUID | None
    created_at: datetime | None


BASELINE = RuleVersion(
    version=0,
    steps=0,
    min_micro_payments=MIN_MICRO_PAYMENTS,
    min_micro_share=MIN_MICRO_SHARE,
    min_distinct_cards=MIN_DISTINCT_CARDS,
    reason="baseline",
    feedback_id=None,
    created_at=None,
)


def clamp_steps(steps: int) -> int:
    return max(0, min(steps, MAX_STEPS))


def thresholds_for_steps(steps: int) -> tuple[int, float, int]:
    step = clamp_steps(steps)
    return (
        MIN_MICRO_PAYMENTS + 2 * step,
        round(min(MIN_MICRO_SHARE + 0.03 * step, MAX_MICRO_SHARE), 2),
        MIN_DISTINCT_CARDS + step,
    )


def _to_rule(row: FraudRuleVersion) -> RuleVersion:
    return RuleVersion(
        version=row.version,
        steps=row.steps,
        min_micro_payments=row.min_micro_payments,
        min_micro_share=row.min_micro_share,
        min_distinct_cards=row.min_distinct_cards,
        reason=row.reason,
        feedback_id=row.feedback_id,
        created_at=row.created_at,
    )


def active_rule_version(session: Session, scope: Scope) -> RuleVersion:
    row = session.execute(
        select(FraudRuleVersion)
        .where(FraudRuleVersion.merchant_id == scope.merchant_id)
        .order_by(FraudRuleVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return BASELINE if row is None else _to_rule(row)


def rule_history(session: Session, scope: Scope) -> list[RuleVersion]:
    rows = session.execute(
        select(FraudRuleVersion)
        .where(FraudRuleVersion.merchant_id == scope.merchant_id)
        .order_by(FraudRuleVersion.version.desc())
    ).scalars()
    return [_to_rule(row) for row in rows]


def is_flagged(episode: CardTestingEpisode, rule: RuleVersion) -> bool:
    return (
        episode.micro_payments >= rule.min_micro_payments
        and episode.micro_share >= rule.min_micro_share
        and (episode.distinct_cards is None or episode.distinct_cards >= rule.min_distinct_cards)
    )


def append_rule_version(
    session: Session,
    scope: Scope,
    clock: Clock,
    steps: int,
    values: tuple[int, float, int],
    reason: str,
    feedback_id: uuid.UUID | None,
) -> RuleVersion:
    """Inserts the next version of the merchant's rule. Does not commit."""
    active = active_rule_version(session, scope)
    min_micro_payments, min_micro_share, min_distinct_cards = values
    row = FraudRuleVersion(
        merchant_id=scope.merchant_id,
        version=active.version + 1,
        steps=clamp_steps(steps),
        min_micro_payments=min_micro_payments,
        min_micro_share=min_micro_share,
        min_distinct_cards=min_distinct_cards,
        reason=reason,
        feedback_id=feedback_id,
        created_at=clock(),
    )
    session.add(row)
    session.flush()
    return _to_rule(row)


def revert_rule(session: Session, scope: Scope, clock: Clock, version: int) -> RuleVersion | None:
    """Restores `version` (0 = baseline) as a new row; None when the merchant has no such version."""
    if version == 0:
        target = BASELINE
    else:
        row = session.execute(
            select(FraudRuleVersion).where(
                FraudRuleVersion.merchant_id == scope.merchant_id, FraudRuleVersion.version == version
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        target = _to_rule(row)
    values = (target.min_micro_payments, target.min_micro_share, target.min_distinct_cards)
    return append_rule_version(session, scope, clock, target.steps, values, f"revert:v{version}", None)
