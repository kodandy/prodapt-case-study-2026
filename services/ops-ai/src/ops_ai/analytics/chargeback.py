"""Chargeback probability: L2-penalised logistic regression trained on the labelled synthetic dataset.

The synthetic chargeback labels are drawn almost independently of the features, so the claim is calibration
(Brier close to the base-rate Brier), not ranking power; see docs/specs/analytics.md section 2.
"""

import functools
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from ops_ai.settlement.models import SettlementEntry
from scripts.generate_dataset import ScenarioLog, generate

FEATURE_NAMES: tuple[str, ...] = ("log_amount_usd", "cross_border", "prior_decline_24h", "merchant_cb_rate")
PRIOR_RATE = 0.006
PRIOR_WEIGHT = 50
SCORING_UNITS_PER_USD: dict[str, float] = {"USD": 1.0, "EUR": 0.92, "INR": 84.0, "JPY": 150.0}
SCORING_MINOR_PER_UNIT: dict[str, int] = {"USD": 100, "EUR": 100, "INR": 100, "JPY": 1}
DEFAULT_TRAIN_FRACTION = 0.7
DEFAULT_LOOKBACK_DAYS = 30
MAX_SCORED_PAYMENTS = 2000
DECLINE_WINDOW = timedelta(hours=24)
DATASET_SEED = 42
DATASET_RECORDS = 12000
_CAPTURED_STATUSES = ("success", "reversed")
_DECLINE_STATUSES = ("failed", "flagged")
_CONVERGENCE_TOLERANCE = 1e-10


@dataclass(frozen=True)
class ChargebackFeatures:
    amount_usd: float
    cross_border: bool | None
    prior_decline_24h: bool
    merchant_cb_rate: float


@dataclass(frozen=True)
class LogisticModel:
    intercept: float
    weights: tuple[float, ...]
    cross_border_mean: float


@dataclass(frozen=True)
class LabeledRow:
    features: ChargebackFeatures
    label: bool
    occurred_at: datetime


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    mean_predicted: float
    observed_rate: float


@dataclass(frozen=True)
class ChargebackEvaluation:
    model: LogisticModel
    train_count: int
    test_count: int
    test_positives: int
    auc: float | None
    brier: float
    base_rate_brier: float
    calibration: tuple[CalibrationBin, ...]


@dataclass(frozen=True)
class PaymentRisk:
    payment_id: uuid.UUID
    amount_minor: int
    currency: str
    occurred_at: datetime
    probability: float
    contributions: dict[str, float]


@dataclass(frozen=True)
class MerchantChargebackRisk:
    captured_count: int
    chargeback_count: int
    merchant_cb_rate: float
    items: tuple[PaymentRisk, ...]


def smoothed_rate(chargebacks: int, captured: int) -> float:
    return (chargebacks + PRIOR_WEIGHT * PRIOR_RATE) / (captured + PRIOR_WEIGHT)


def amount_in_usd(amount_minor: int, currency: str) -> float:
    """Unknown currencies are read as two-decimal, USD-valued amounts."""
    minor_per_unit = SCORING_MINOR_PER_UNIT.get(currency, 100)
    units_per_usd = SCORING_UNITS_PER_USD.get(currency, 1.0)
    return amount_minor / minor_per_unit / units_per_usd


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    exp_z = math.exp(z)
    return exp_z / (1.0 + exp_z)


def feature_vector(model: LogisticModel, features: ChargebackFeatures) -> tuple[float, ...]:
    cross_border = model.cross_border_mean if features.cross_border is None else float(features.cross_border)
    return (
        math.log1p(features.amount_usd),
        cross_border,
        float(features.prior_decline_24h),
        features.merchant_cb_rate,
    )


def _logit(model: LogisticModel, vector: Sequence[float]) -> float:
    return model.intercept + sum(w * x for w, x in zip(model.weights, vector, strict=True))


def score(model: LogisticModel, features: ChargebackFeatures) -> float:
    return _sigmoid(_logit(model, feature_vector(model, features)))


def contributions(model: LogisticModel, features: ChargebackFeatures) -> dict[str, float]:
    vector = feature_vector(model, features)
    return {name: w * x for name, w, x in zip(FEATURE_NAMES, model.weights, vector, strict=True)}


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting; the penalised Hessian is positive definite."""
    size = len(rhs)
    a = [[*row, rhs[i]] for i, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if a[col][col] == 0.0:
            raise ValueError("singular system")
        for r in range(col + 1, size):
            factor = a[r][col] / a[col][col]
            if factor != 0.0:
                for c in range(col, size + 1):
                    a[r][c] -= factor * a[col][c]
    solution = [0.0] * size
    for r in range(size - 1, -1, -1):
        total = a[r][size] - sum(a[r][c] * solution[c] for c in range(r + 1, size))
        solution[r] = total / a[r][r]
    return solution


def fit_logistic(rows: Sequence[LabeledRow], l2: float = 1.0, iterations: int = 25) -> LogisticModel:
    """Newton (IRLS) on the penalised negative log-likelihood; the intercept is not penalised."""
    known = [r.features.cross_border for r in rows if r.features.cross_border is not None]
    cross_border_mean = sum(known) / len(known) if known else 0.0
    template = LogisticModel(
        intercept=0.0, weights=(0.0,) * len(FEATURE_NAMES), cross_border_mean=cross_border_mean
    )
    design = [(1.0, *feature_vector(template, r.features)) for r in rows]
    targets = [1.0 if r.label else 0.0 for r in rows]
    size = len(FEATURE_NAMES) + 1
    beta = [0.0] * size
    for _ in range(iterations):
        gradient = [l2 * beta[j] if j > 0 else 0.0 for j in range(size)]
        hessian = [[(l2 if i == j and i > 0 else 0.0) for j in range(size)] for i in range(size)]
        for x, y in zip(design, targets, strict=True):
            p = _sigmoid(sum(b * v for b, v in zip(beta, x, strict=True)))
            residual = p - y
            weight = p * (1.0 - p)
            for i in range(size):
                gradient[i] += residual * x[i]
                wx = weight * x[i]
                for j in range(i, size):
                    hessian[i][j] += wx * x[j]
        for i in range(size):
            for j in range(i):
                hessian[i][j] = hessian[j][i]
        step = _solve(hessian, gradient)
        beta = [b - s for b, s in zip(beta, step, strict=True)]
        if max(abs(s) for s in step) < _CONVERGENCE_TOLERANCE:
            break
    return LogisticModel(intercept=beta[0], weights=tuple(beta[1:]), cross_border_mean=cross_border_mean)


def roc_auc(scores: Sequence[float], labels: Sequence[bool]) -> float:
    """Mann-Whitney statistic via average ranks; ties count one half."""
    positives = sum(1 for label in labels if label)
    negatives = len(labels) - positives
    if positives == 0 or negatives == 0:
        raise ValueError("roc_auc needs both classes")
    order = sorted(range(len(scores)), key=lambda i: scores[i])
    ranks = [0.0] * len(scores)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[start]]:
            end += 1
        average = (start + end) / 2 + 1
        for k in range(start, end + 1):
            ranks[order[k]] = average
        start = end + 1
    positive_rank_sum = sum(rank for rank, label in zip(ranks, labels, strict=True) if label)
    return (positive_rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def brier(scores: Sequence[float], labels: Sequence[bool]) -> float:
    if not scores:
        raise ValueError("brier needs at least one score")
    return sum((s - (1.0 if y else 0.0)) ** 2 for s, y in zip(scores, labels, strict=True)) / len(scores)


def calibration_bins(scores: Sequence[float], labels: Sequence[bool], bins: int = 5) -> list[CalibrationBin]:
    counts = [0] * bins
    predicted = [0.0] * bins
    observed = [0] * bins
    for s, y in zip(scores, labels, strict=True):
        index = min(max(int(s * bins), 0), bins - 1)
        counts[index] += 1
        predicted[index] += s
        observed[index] += int(y)
    return [
        CalibrationBin(
            lower=i / bins,
            upper=(i + 1) / bins,
            count=counts[i],
            mean_predicted=predicted[i] / counts[i],
            observed_rate=observed[i] / counts[i],
        )
        for i in range(bins)
        if counts[i] > 0
    ]


def dataset_rows(log: ScenarioLog) -> list[LabeledRow]:
    """Every feature of a row uses only records before it in (timestamp, transaction_id) order."""
    ordered = sorted(log.transactions, key=lambda tx: (tx.timestamp, tx.transaction_id))
    declines_by_payer: dict[uuid.UUID, list[datetime]] = {}
    captured_by_merchant: dict[str | None, int] = {}
    chargebacks_by_merchant: dict[str | None, int] = {}
    rows: list[LabeledRow] = []
    for tx in ordered:
        if (
            tx.record_kind == "merchant_payment"
            and tx.payment_method == "card"
            and tx.status in _CAPTURED_STATUSES
        ):
            captured = captured_by_merchant.get(tx.merchant_id, 0)
            chargebacks = chargebacks_by_merchant.get(tx.merchant_id, 0)
            recent = declines_by_payer.get(tx.user_id, [])
            features = ChargebackFeatures(
                amount_usd=amount_in_usd(tx.amount_minor, tx.currency),
                cross_border=tx.country_sender != tx.country_receiver,
                prior_decline_24h=any(tx.timestamp - DECLINE_WINDOW <= at < tx.timestamp for at in recent),
                merchant_cb_rate=smoothed_rate(chargebacks, captured),
            )
            rows.append(LabeledRow(features=features, label=tx.chargeback_flag, occurred_at=tx.timestamp))
            captured_by_merchant[tx.merchant_id] = captured + 1
            chargebacks_by_merchant[tx.merchant_id] = chargebacks + int(tx.chargeback_flag)
        if tx.status in _DECLINE_STATUSES:
            declines_by_payer.setdefault(tx.user_id, []).append(tx.timestamp)
    return rows


def evaluate_on_dataset(
    log: ScenarioLog, train_fraction: float = DEFAULT_TRAIN_FRACTION
) -> ChargebackEvaluation:
    rows = dataset_rows(log)
    train_count = int(len(rows) * train_fraction)
    train, test = rows[:train_count], rows[train_count:]
    if not train or not test:
        raise ValueError("the dataset is too small to split")
    model = fit_logistic(train)
    scores = [score(model, r.features) for r in test]
    labels = [r.label for r in test]
    positives = sum(labels)
    rate = sum(r.label for r in train) / len(train)
    return ChargebackEvaluation(
        model=model,
        train_count=len(train),
        test_count=len(test),
        test_positives=positives,
        auc=roc_auc(scores, labels) if 0 < positives < len(test) else None,
        brier=brier(scores, labels),
        base_rate_brier=brier([rate] * len(test), labels),
        calibration=tuple(calibration_bins(scores, labels)),
    )


@functools.cache
def default_evaluation() -> ChargebackEvaluation:
    return evaluate_on_dataset(generate(seed=DATASET_SEED, records=DATASET_RECORDS))


def score_merchant_payments(
    session: Session,
    scope: Scope,
    now: datetime,
    model: LogisticModel,
    limit: int,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> MerchantChargebackRisk:
    """Serving has no country pair, so cross_border is None and the model's training mean is imputed."""
    counts = session.execute(
        select(func.count(), func.count().filter(SettlementEntry.chargeback)).where(
            SettlementEntry.merchant_id == scope.merchant_id, SettlementEntry.captured_at < now
        )
    ).one()
    captured_count, chargeback_count = int(counts[0]), int(counts[1])
    start = now - timedelta(days=lookback_days)
    payments = session.scalars(
        select(PaymentFact)
        .where(
            PaymentFact.merchant_id == scope.merchant_id,
            PaymentFact.state == "succeeded",
            PaymentFact.occurred_at >= start,
            PaymentFact.occurred_at < now,
        )
        .order_by(PaymentFact.occurred_at.desc(), PaymentFact.payment_id)
        .limit(MAX_SCORED_PAYMENTS)
    ).all()
    payment_ids = [p.payment_id for p in payments]
    own_entries = {
        payment_id: chargeback
        for payment_id, chargeback in session.execute(
            select(SettlementEntry.payment_id, SettlementEntry.chargeback).where(
                SettlementEntry.merchant_id == scope.merchant_id,
                SettlementEntry.captured_at < now,
                SettlementEntry.payment_id.in_(payment_ids),
            )
        ).all()
    }
    card_refs = {p.card_ref for p in payments if p.card_ref is not None}
    declines: dict[str, list[datetime]] = {}
    if card_refs:
        for card_ref, occurred_at in session.execute(
            select(PaymentFact.card_ref, PaymentFact.occurred_at).where(
                PaymentFact.merchant_id == scope.merchant_id,
                PaymentFact.state == "declined",
                PaymentFact.card_ref.in_(card_refs),
                PaymentFact.occurred_at >= start - DECLINE_WINDOW,
                PaymentFact.occurred_at < now,
            )
        ).all():
            if card_ref is not None:
                declines.setdefault(card_ref, []).append(occurred_at)
    items: list[PaymentRisk] = []
    for payment in payments:
        own = own_entries.get(payment.payment_id)
        rate = smoothed_rate(
            chargeback_count - (1 if own else 0),
            captured_count - (0 if own is None else 1),
        )
        prior = payment.card_ref is not None and any(
            payment.occurred_at - DECLINE_WINDOW <= at < payment.occurred_at
            for at in declines.get(payment.card_ref, [])
        )
        features = ChargebackFeatures(
            amount_usd=amount_in_usd(payment.amount_minor, payment.currency),
            cross_border=None,
            prior_decline_24h=prior,
            merchant_cb_rate=rate,
        )
        items.append(
            PaymentRisk(
                payment_id=payment.payment_id,
                amount_minor=payment.amount_minor,
                currency=payment.currency,
                occurred_at=payment.occurred_at,
                probability=score(model, features),
                contributions=contributions(model, features),
            )
        )
    items.sort(key=lambda item: (-item.probability, item.payment_id))
    return MerchantChargebackRisk(
        captured_count=captured_count,
        chargeback_count=chargeback_count,
        merchant_cb_rate=smoothed_rate(chargeback_count, captured_count),
        items=tuple(items[:limit]),
    )
