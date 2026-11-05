"""B7.3: false-positive measurement of the card risk rule on a fixed, labeled set of micro-payment groups.

Run `python -m gateway.payments.risk_eval` to print the result for the default seed.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from gateway.payments.risk import (
    DECLINE_WINDOW,
    VELOCITY_WINDOW,
    RiskAssessment,
    score_activity,
)

__all__ = [
    "DEFAULT_SEED",
    "LabeledCharge",
    "LabeledGroup",
    "RiskEvalResult",
    "labeled_groups",
    "measure",
    "replay",
    "score_activity",
]

DEFAULT_SEED = 20261103
GroupLabel = Literal["legit", "burst"]

_LEGIT_GROUPS = 160
_BURST_GROUPS = 40
_MICRO_AMOUNTS = {"USD": (99, 1_500), "EUR": (99, 1_500), "JPY": (100, 1_500), "INR": (1_000, 50_000)}


@dataclass(frozen=True)
class LabeledCharge:
    offset_seconds: float
    """Seconds since the group's first charge."""
    amount_minor: int
    currency: str
    declined: bool


@dataclass(frozen=True)
class LabeledGroup:
    """Chronological charges on one card."""

    label: GroupLabel
    kind: str
    charges: tuple[LabeledCharge, ...]


@dataclass(frozen=True)
class RiskEvalResult:
    legit_groups: int
    burst_groups: int
    false_positives: int
    detected: int
    false_positive_rate: float
    detection_rate: float


def _charges(
    rng: random.Random, count: int, gap: tuple[int, int], amounts: tuple[int, int], currency: str, p: float
) -> tuple[LabeledCharge, ...]:
    offset = 0
    charges: list[LabeledCharge] = []
    for index in range(count):
        if index:
            offset += rng.randint(*gap)
        charges.append(
            LabeledCharge(
                offset_seconds=float(offset),
                amount_minor=rng.randint(*amounts),
                currency=currency,
                declined=rng.random() < p,
            )
        )
    return tuple(charges)


def _legit_group(rng: random.Random) -> LabeledGroup:
    currency = rng.choice(sorted(_MICRO_AMOUNTS))
    low, high = _MICRO_AMOUNTS[currency]
    kind = rng.choice(("transit_taps", "coffee_runs", "in_app_purchases"))
    if kind == "transit_taps":
        charges = _charges(rng, rng.randint(2, 6), (300, 3_600), (low, low * 3), currency, 0.03)
    elif kind == "coffee_runs":
        charges = _charges(rng, rng.randint(1, 3), (3_600, 14_400), (low * 2, high), currency, 0.03)
    else:
        charges = _charges(rng, rng.randint(3, 8), (30, 180), (low, high), currency, 0.10)
    return LabeledGroup(label="legit", kind=kind, charges=charges)


def _burst_group(rng: random.Random) -> LabeledGroup:
    """Card testing: many sub-$2 USD charges a few seconds apart, mostly declined."""
    while True:
        charges = _charges(rng, rng.randint(6, 25), (5, 40), (50, 199), "USD", 0.85)
        declined = sum(c.declined for c in charges)
        if declined / len(charges) > 0.5 and sum(c.declined for c in charges[:3]) >= 2:
            return LabeledGroup(label="burst", kind="card_testing", charges=charges)


def labeled_groups(seed: int = DEFAULT_SEED) -> list[LabeledGroup]:
    """Deterministic for one seed; not a security use of randomness."""
    rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data set, not a secret
    groups = [_legit_group(rng) for _ in range(_LEGIT_GROUPS)]
    groups.extend(_burst_group(rng) for _ in range(_BURST_GROUPS))
    rng.shuffle(groups)
    return groups


def replay(group: LabeledGroup) -> list[RiskAssessment]:
    """Scores each charge against the earlier charges of its group with `card_activity`'s windows."""
    velocity = VELOCITY_WINDOW.total_seconds()
    decline = DECLINE_WINDOW.total_seconds()
    assessments: list[RiskAssessment] = []
    for index, charge in enumerate(group.charges):
        now = charge.offset_seconds
        earlier = group.charges[:index]
        recent = sum(1 for c in earlier if c.offset_seconds >= now - velocity)
        declines = sum(1 for c in earlier if c.declined and c.offset_seconds >= now - decline)
        assessments.append(score_activity(charge.amount_minor, charge.currency, recent, declines))
    return assessments


def _positive(group: LabeledGroup) -> bool:
    return any(a.flagged or a.blocked for a in replay(group))


def measure(groups: Sequence[LabeledGroup]) -> RiskEvalResult:
    """A group is positive if any of its charges is flagged or blocked."""
    legit = [g for g in groups if g.label == "legit"]
    bursts = [g for g in groups if g.label == "burst"]
    false_positives = sum(1 for g in legit if _positive(g))
    detected = sum(1 for g in bursts if _positive(g))
    return RiskEvalResult(
        legit_groups=len(legit),
        burst_groups=len(bursts),
        false_positives=false_positives,
        detected=detected,
        false_positive_rate=false_positives / len(legit) if legit else 0.0,
        detection_rate=detected / len(bursts) if bursts else 0.0,
    )


def _main() -> None:
    result = measure(labeled_groups(DEFAULT_SEED))
    print(f"seed={DEFAULT_SEED}")
    for name, value in vars(result).items():
        print(f"{name}={value}")


if __name__ == "__main__":
    _main()
