"""Per-merchant, versioned card-testing thresholds driven by ops feedback (R2-7).

Every change is a new append-only row, so the history is the audit trail and any version can be restored.
Each row also records the acting principal (the token scope that wrote it) in `fraud_rule_version_actors`,
including the acting API key when the verified token names one (`actor_identity_of`).
The ladder only tightens: no step is looser than the shipped baseline in `fraud_watch.rules`.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import Row, Select, select
from sqlalchemy.orm import Session

from ops_ai.clock import Clock
from ops_ai.feedback.models import FraudRuleVersion, FraudRuleVersionActor
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


@dataclass(frozen=True)
class RuleAuditEntry:
    version: int
    reason: str
    feedback_id: uuid.UUID | None
    actor: str | None
    """None only for a version row without an actor row (written before actors were recorded)."""
    actor_role: str | None
    created_at: datetime


ActorKind = Literal["api_key", "merchant_principal"]


@dataclass(frozen=True)
class RuleActorIdentity:
    """Who acted, finer than the principal: the gateway API key, or the merchant principal if no key is known.
    Merchants have no user accounts, so an API key is the finest acting identity; it is never guessed."""

    actor_kind: ActorKind
    actor_id: uuid.UUID | None


@dataclass(frozen=True)
class KeysetPage[T]:
    items: list[T]
    next_before: int | None
    """The last item's version when more rows exist below it, else None."""


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


def actor_of(scope: Scope) -> str:
    """The token subject of a merchant principal; merchant tokens carry no per-user id."""
    return f"merchant:{scope.merchant_id}"


def actor_identity_of(scope: Scope) -> RuleActorIdentity:
    """The API key named by the verified token, else the merchant principal with no id."""
    if scope.api_key_id is None:
        return RuleActorIdentity(actor_kind="merchant_principal", actor_id=None)
    return RuleActorIdentity(actor_kind="api_key", actor_id=scope.api_key_id)


def rule_actor_identity(session: Session, scope: Scope, version: int) -> RuleActorIdentity | None:
    """The acting identity of the scope merchant's `version`; None if it has no actor row (or is not theirs).
    A row written before 11-06 reads as the merchant principal with no id."""
    row = session.execute(
        select(FraudRuleVersionActor.actor_kind, FraudRuleVersionActor.actor_id).where(
            FraudRuleVersionActor.merchant_id == scope.merchant_id, FraudRuleVersionActor.version == version
        )
    ).one_or_none()
    if row is None:
        return None
    if row.actor_kind == "api_key" and row.actor_id is not None:
        return RuleActorIdentity(actor_kind="api_key", actor_id=row.actor_id)
    if row.actor_kind == "merchant_principal" and row.actor_id is None:
        return RuleActorIdentity(actor_kind="merchant_principal", actor_id=None)
    raise ValueError("inconsistent stored actor identity")


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


def _history_query(scope: Scope) -> Select[FraudRuleVersion]:
    return (
        select(FraudRuleVersion)
        .where(FraudRuleVersion.merchant_id == scope.merchant_id)
        .order_by(FraudRuleVersion.version.desc())
    )


def _audit_query(scope: Scope) -> Select[int, str, uuid.UUID | None, datetime, str, str]:
    """The actor columns come from an outer join, so they are None for a version without an actor row."""
    return (
        select(
            FraudRuleVersion.version,
            FraudRuleVersion.reason,
            FraudRuleVersion.feedback_id,
            FraudRuleVersion.created_at,
            FraudRuleVersionActor.actor,
            FraudRuleVersionActor.actor_role,
        )
        .outerjoin(
            FraudRuleVersionActor,
            (FraudRuleVersionActor.merchant_id == FraudRuleVersion.merchant_id)
            & (FraudRuleVersionActor.version == FraudRuleVersion.version),
        )
        .where(FraudRuleVersion.merchant_id == scope.merchant_id)
        .order_by(FraudRuleVersion.version.desc())
    )


def _to_audit_entry(row: Row[int, str, uuid.UUID | None, datetime, str, str]) -> RuleAuditEntry:
    return RuleAuditEntry(
        version=row.version,
        reason=row.reason,
        feedback_id=row.feedback_id,
        actor=row.actor,
        actor_role=row.actor_role,
        created_at=row.created_at,
    )


def _keyset_page[T](items: list[T], limit: int, version_of: Callable[[T], int]) -> KeysetPage[T]:
    page = items[:limit]
    next_before = version_of(page[-1]) if len(items) > limit else None
    return KeysetPage(items=page, next_before=next_before)


def rule_history(session: Session, scope: Scope) -> list[RuleVersion]:
    return [_to_rule(row) for row in session.execute(_history_query(scope)).scalars()]


def rule_history_page(
    session: Session, scope: Scope, limit: int, before: int | None
) -> KeysetPage[RuleVersion]:
    """At most `limit` of the scope merchant's versions below `before` (all when None), newest first."""
    query = _history_query(scope)
    if before is not None:
        query = query.where(FraudRuleVersion.version < before)
    rules = [_to_rule(row) for row in session.execute(query.limit(limit + 1)).scalars()]
    return _keyset_page(rules, limit, lambda rule: rule.version)


def rule_audit(session: Session, scope: Scope) -> list[RuleAuditEntry]:
    return [_to_audit_entry(row) for row in session.execute(_audit_query(scope))]


def rule_audit_page(
    session: Session, scope: Scope, limit: int, before: int | None
) -> KeysetPage[RuleAuditEntry]:
    """At most `limit` of the scope merchant's audit entries below `before` (all when None), newest first."""
    query = _audit_query(scope)
    if before is not None:
        query = query.where(FraudRuleVersion.version < before)
    entries = [_to_audit_entry(row) for row in session.execute(query.limit(limit + 1))]
    return _keyset_page(entries, limit, lambda entry: entry.version)


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
    """Inserts the next version of the merchant's rule and its actor (`scope`). Does not commit."""
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
    identity = actor_identity_of(scope)
    session.add(
        FraudRuleVersionActor(
            merchant_id=scope.merchant_id,
            version=row.version,
            actor=actor_of(scope),
            actor_role=scope.role,
            actor_kind=identity.actor_kind,
            actor_id=identity.actor_id,
            created_at=row.created_at,
        )
    )
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
