"""Settlement forecast: simple exponential smoothing of daily captured card volume per currency.

The synthetic dataset has no weekly seasonality, so a level-only model is used; alpha is fitted per series.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope

ALPHA_GRID: tuple[float, ...] = tuple(round(0.1 * step, 1) for step in range(1, 10))
INTERVAL_Z = 1.96
DEFAULT_ALPHA = 0.5
MIN_TRAINING_POINTS = 7
BACKTEST_FOLDS = 3
DEFAULT_HISTORY_DAYS = 28
CAPTURED_STATE = "succeeded"


@dataclass(frozen=True)
class DailyPoint:
    day: date
    amount_minor: int


@dataclass(frozen=True)
class ForecastPoint:
    day: date
    expected_minor: int
    low_minor: int
    high_minor: int


@dataclass(frozen=True)
class BacktestResult:
    horizon_days: int
    folds: int
    mae_minor: float
    wape: float | None
    naive_wape: float | None


@dataclass(frozen=True)
class CurrencyForecast:
    currency: str
    alpha: float
    history: tuple[DailyPoint, ...]
    forecast: tuple[ForecastPoint, ...]
    backtest: BacktestResult | None


def fill_daily(points: Sequence[DailyPoint], start: date, end: date) -> list[DailyPoint]:
    by_day: dict[date, int] = {}
    for point in points:
        if start <= point.day < end:
            by_day[point.day] = by_day.get(point.day, 0) + point.amount_minor
    days = [start + timedelta(days=i) for i in range(max(0, (end - start).days))]
    return [DailyPoint(day=day, amount_minor=by_day.get(day, 0)) for day in days]


def _one_step(values: Sequence[int], alpha: float) -> tuple[float, list[float]]:
    """The final level and the one-step-ahead residuals."""
    level = float(values[0])
    residuals: list[float] = []
    for value in values[1:]:
        residuals.append(value - level)
        level = alpha * value + (1 - alpha) * level
    return level, residuals


def fit_alpha(values: Sequence[int]) -> float:
    if len(values) < 3:
        return DEFAULT_ALPHA
    best_alpha, best_error = ALPHA_GRID[0], math.inf
    for alpha in ALPHA_GRID:
        _level, residuals = _one_step(values, alpha)
        error = sum(r * r for r in residuals)
        if error < best_error:
            best_alpha, best_error = alpha, error
    return best_alpha


def forecast_series(
    history: Sequence[DailyPoint], horizon_days: int, alpha: float | None = None
) -> list[ForecastPoint]:
    if not history:
        return []
    values = [p.amount_minor for p in history]
    level, residuals = _one_step(values, fit_alpha(values) if alpha is None else alpha)
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else 0.0
    half = INTERVAL_Z * rmse
    expected, low, high = round(level), max(0, round(level - half)), round(level + half)
    last = history[-1].day
    return [
        ForecastPoint(day=last + timedelta(days=i), expected_minor=expected, low_minor=low, high_minor=high)
        for i in range(1, horizon_days + 1)
    ]


def backtest(history: Sequence[DailyPoint], horizon_days: int, folds: int) -> BacktestResult | None:
    """Rolling origin: fold k trains on history[: n - k*horizon] and tests the next horizon days."""
    model_errors: list[int] = []
    naive_errors: list[int] = []
    actual_total = 0
    used = 0
    for k in range(1, folds + 1):
        cut = len(history) - k * horizon_days
        if cut < MIN_TRAINING_POINTS:
            continue
        train, test = history[:cut], history[cut : cut + horizon_days]
        naive = train[-1].amount_minor
        for point, guess in zip(test, forecast_series(train, horizon_days), strict=True):
            model_errors.append(abs(point.amount_minor - guess.expected_minor))
            naive_errors.append(abs(point.amount_minor - naive))
            actual_total += point.amount_minor
        used += 1
    if used == 0:
        return None
    return BacktestResult(
        horizon_days=horizon_days,
        folds=used,
        mae_minor=sum(model_errors) / len(model_errors),
        wape=sum(model_errors) / actual_total if actual_total > 0 else None,
        naive_wape=sum(naive_errors) / actual_total if actual_total > 0 else None,
    )


def merchant_forecast(
    session: Session,
    scope: Scope,
    now: datetime,
    horizon_days: int,
    history_days: int = DEFAULT_HISTORY_DAYS,
) -> list[CurrencyForecast]:
    """History = the `history_days` full UTC days before `now`'s day (today is partial and excluded)."""
    end_day = now.astimezone(UTC).date()
    start_day = end_day - timedelta(days=history_days)
    day_expr = cast(func.timezone("UTC", PaymentFact.occurred_at), Date)
    rows = session.execute(
        select(PaymentFact.currency, day_expr, func.sum(PaymentFact.amount_minor))
        .where(
            PaymentFact.merchant_id == scope.merchant_id,
            PaymentFact.state == CAPTURED_STATE,
            PaymentFact.occurred_at >= datetime.combine(start_day, time(), tzinfo=UTC),
            PaymentFact.occurred_at < datetime.combine(end_day, time(), tzinfo=UTC),
        )
        .group_by(PaymentFact.currency, day_expr)
    ).all()
    per_currency: dict[str, list[DailyPoint]] = {}
    for currency, day, total in rows:
        per_currency.setdefault(str(currency), []).append(DailyPoint(day=day, amount_minor=int(total)))
    result: list[CurrencyForecast] = []
    for currency in sorted(per_currency):
        history = fill_daily(per_currency[currency], start_day, end_day)
        alpha = fit_alpha([p.amount_minor for p in history])
        result.append(
            CurrencyForecast(
                currency=currency,
                alpha=alpha,
                history=tuple(history),
                forecast=tuple(forecast_series(history, horizon_days, alpha=alpha)),
                backtest=backtest(history, horizon_days, BACKTEST_FOLDS),
            )
        )
    return result
