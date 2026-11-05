"""Tests for ops_ai.analytics.forecast, written from docs/specs/analytics.md section 1."""

import random
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from contracts.events import PaymentEventV1
from ops_ai.analytics.forecast import (
    ALPHA_GRID,
    INTERVAL_Z,
    DailyPoint,
    ForecastPoint,
    backtest,
    fill_daily,
    fit_alpha,
    forecast_series,
    merchant_forecast,
)
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import Scope
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

FIRST_DAY = date(2026, 10, 1)
MERCHANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
MERCHANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b2")


def _history(values: Sequence[int], first_day: date = FIRST_DAY) -> list[DailyPoint]:
    return [DailyPoint(day=first_day + timedelta(days=i), amount_minor=v) for i, v in enumerate(values)]


def _squared_error(values: Sequence[int], alpha: float) -> float:
    level = float(values[0])
    total = 0.0
    for value in values[1:]:
        total += (value - level) ** 2
        level = alpha * value + (1 - alpha) * level
    return total


# --- ALPHA_GRID / INTERVAL_Z -----------------------------------------------------------------------------


def test_alpha_grid_is_the_nine_values_from_one_tenth_to_nine_tenths() -> None:
    # Arrange
    expected = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

    # Act
    grid = ALPHA_GRID

    # Assert
    assert grid == pytest.approx(expected)


def test_interval_z_is_the_two_sided_95_percent_normal_quantile() -> None:
    # Arrange / Act
    z = INTERVAL_Z

    # Assert
    assert z == 1.96


# --- fill_daily ------------------------------------------------------------------------------------------


def test_fill_daily_returns_one_point_per_day_in_order_with_missing_days_as_zero() -> None:
    # Arrange
    points = [
        DailyPoint(day=date(2026, 10, 4), amount_minor=400),
        DailyPoint(day=date(2026, 10, 2), amount_minor=200),
    ]

    # Act
    filled = fill_daily(points, date(2026, 10, 1), date(2026, 10, 5))

    # Assert
    assert filled == [
        DailyPoint(day=date(2026, 10, 1), amount_minor=0),
        DailyPoint(day=date(2026, 10, 2), amount_minor=200),
        DailyPoint(day=date(2026, 10, 3), amount_minor=0),
        DailyPoint(day=date(2026, 10, 4), amount_minor=400),
    ]


def test_fill_daily_drops_points_before_start_and_on_or_after_end() -> None:
    # Arrange
    points = [
        DailyPoint(day=date(2026, 9, 30), amount_minor=999),
        DailyPoint(day=date(2026, 10, 1), amount_minor=111),
        DailyPoint(day=date(2026, 10, 3), amount_minor=333),
        DailyPoint(day=date(2026, 10, 4), amount_minor=555),
    ]

    # Act
    filled = fill_daily(points, date(2026, 10, 1), date(2026, 10, 4))

    # Assert
    assert [(p.day, p.amount_minor) for p in filled] == [
        (date(2026, 10, 1), 111),
        (date(2026, 10, 2), 0),
        (date(2026, 10, 3), 333),
    ]


def test_fill_daily_with_no_points_returns_zeros_for_every_day() -> None:
    # Arrange
    start, end = date(2026, 10, 1), date(2026, 10, 4)

    # Act
    filled = fill_daily([], start, end)

    # Assert
    assert [p.amount_minor for p in filled] == [0, 0, 0]
    assert [p.day for p in filled] == [date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]


def test_fill_daily_with_start_equal_to_end_returns_an_empty_list() -> None:
    # Arrange
    points = [DailyPoint(day=date(2026, 10, 1), amount_minor=5)]

    # Act
    filled = fill_daily(points, date(2026, 10, 1), date(2026, 10, 1))

    # Assert
    assert filled == []


# --- fit_alpha -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("values", [[], [5], [5, 9]], ids=["empty", "one_value", "two_values"])
def test_fit_alpha_with_fewer_than_three_values_returns_one_half(values: list[int]) -> None:
    # Arrange / Act
    alpha = fit_alpha(values)

    # Assert
    assert alpha == 0.5


def test_fit_alpha_with_a_tie_returns_the_smallest_alpha() -> None:
    # Arrange: a constant series has zero error for every alpha
    values = [100] * 10

    # Act
    alpha = fit_alpha(values)

    # Assert
    assert alpha == pytest.approx(0.1)


def test_fit_alpha_with_exactly_three_values_minimises_the_one_step_error() -> None:
    # Arrange: errors are 100 and 200 - 100*alpha, so the largest alpha wins
    values = [0, 100, 200]

    # Act
    alpha = fit_alpha(values)

    # Assert
    assert alpha == pytest.approx(0.9)


def test_fit_alpha_on_a_linear_trend_picks_the_most_reactive_alpha() -> None:
    # Arrange
    values = [0, 100, 200, 300, 400, 500]

    # Act
    alpha = fit_alpha(values)

    # Assert
    assert alpha == pytest.approx(0.9)


def test_fit_alpha_matches_the_grid_argmin_of_the_one_step_squared_error_on_a_noisy_series() -> None:
    # Arrange
    rng = random.Random(3)  # noqa: S311 - reproducible test data
    values = [round(1000 + rng.gauss(0, 150)) for _ in range(30)]
    expected = min(ALPHA_GRID, key=lambda a: _squared_error(values, a))

    # Act
    alpha = fit_alpha(values)

    # Assert
    assert alpha == pytest.approx(expected)


# --- forecast_series -------------------------------------------------------------------------------------


def test_forecast_series_with_empty_history_returns_an_empty_list() -> None:
    # Arrange / Act
    result = forecast_series([], 7, alpha=0.5)

    # Assert
    assert result == []


def test_forecast_series_uses_the_smoothed_level_and_a_symmetric_interval_of_z_times_rmse() -> None:
    # Arrange: level 1000 -> 1050 -> 1125, residuals 100 and 150, rmse = sqrt(16250) = 127.4755
    history = _history([1000, 1100, 1200])

    # Act
    result = forecast_series(history, 3, alpha=0.5)

    # Assert
    assert result == [
        ForecastPoint(day=date(2026, 10, 4), expected_minor=1125, low_minor=875, high_minor=1375),
        ForecastPoint(day=date(2026, 10, 5), expected_minor=1125, low_minor=875, high_minor=1375),
        ForecastPoint(day=date(2026, 10, 6), expected_minor=1125, low_minor=875, high_minor=1375),
    ]


def test_forecast_series_clips_the_lower_bound_at_zero() -> None:
    # Arrange: level 225, half-width 249.85 -> raw low -24.85
    history = _history([100, 200, 300])

    # Act
    result = forecast_series(history, 2, alpha=0.5)

    # Assert
    assert result == [
        ForecastPoint(day=date(2026, 10, 4), expected_minor=225, low_minor=0, high_minor=475),
        ForecastPoint(day=date(2026, 10, 5), expected_minor=225, low_minor=0, high_minor=475),
    ]


def test_forecast_series_with_a_single_point_has_a_zero_width_interval() -> None:
    # Arrange
    history = _history([500])

    # Act
    result = forecast_series(history, 2, alpha=0.3)

    # Assert
    assert result == [
        ForecastPoint(day=date(2026, 10, 2), expected_minor=500, low_minor=500, high_minor=500),
        ForecastPoint(day=date(2026, 10, 3), expected_minor=500, low_minor=500, high_minor=500),
    ]


def test_forecast_series_with_two_points_uses_the_single_residual_as_rmse() -> None:
    # Arrange: level 200, one residual of 200, half-width 392
    history = _history([100, 300])

    # Act
    result = forecast_series(history, 1, alpha=0.5)

    # Assert
    assert result == [ForecastPoint(day=date(2026, 10, 3), expected_minor=200, low_minor=0, high_minor=592)]


def test_forecast_series_returns_whole_minor_units() -> None:
    # Arrange
    history = _history([1000, 1100, 1200])

    # Act
    result = forecast_series(history, 1, alpha=0.5)

    # Assert
    assert [type(v) for v in (result[0].expected_minor, result[0].low_minor, result[0].high_minor)] == [
        int,
        int,
        int,
    ]


def test_forecast_series_without_alpha_uses_the_fitted_alpha() -> None:
    # Arrange: fit_alpha is 0.9 on this trend, so the level is 488.889
    history = _history([0, 100, 200, 300, 400, 500])

    # Act
    fitted = forecast_series(history, 2)
    explicit = forecast_series(history, 2, alpha=0.9)

    # Assert
    assert fitted == explicit
    assert fitted[0].expected_minor == 489


def test_forecast_series_days_follow_the_last_history_day() -> None:
    # Arrange
    history = _history([10, 20, 30, 40], first_day=date(2026, 12, 29))

    # Act
    result = forecast_series(history, 3, alpha=0.5)

    # Assert
    assert [p.day for p in result] == [date(2027, 1, 2), date(2027, 1, 3), date(2027, 1, 4)]


# --- backtest --------------------------------------------------------------------------------------------


def test_backtest_with_constant_training_parts_has_exact_errors_and_skips_short_folds() -> None:
    # Arrange: n=12, horizon 2, folds 3 -> training sizes 10, 8, 6; the 6-point fold is skipped.
    # Every training part is the constant 100, so the forecast is 100 for any alpha.
    history = _history([100] * 10 + [120, 80])

    # Act
    result = backtest(history, 2, 3)

    # Assert: fold 1 errors 20, 20; fold 2 errors 0, 0 -> mae 40/4, wape 40/400
    assert result is not None
    assert (result.horizon_days, result.folds) == (2, 2)
    assert result.mae_minor == pytest.approx(10.0)
    assert result.wape == pytest.approx(0.1)
    assert result.naive_wape == pytest.approx(0.1)


def test_backtest_accepts_a_training_part_of_exactly_seven_points() -> None:
    # Arrange: n=9, horizon 2, folds 1 -> training size 7
    history = _history([100] * 7 + [130, 70])

    # Act
    result = backtest(history, 2, 1)

    # Assert
    assert result is not None
    assert (result.horizon_days, result.folds) == (2, 1)
    assert result.mae_minor == pytest.approx(30.0)
    assert result.wape == pytest.approx(0.3)
    assert result.naive_wape == pytest.approx(0.3)


def test_backtest_returns_none_when_every_training_part_has_fewer_than_seven_points() -> None:
    # Arrange: n=8, horizon 2, folds 3 -> training sizes 6, 4, 2
    history = _history([100] * 8)

    # Act
    result = backtest(history, 2, 3)

    # Assert
    assert result is None


def test_backtest_returns_none_when_the_horizon_exceeds_the_history() -> None:
    # Arrange: training sizes 12-30 < 0 for every fold
    history = _history([100] * 12)

    # Act
    result = backtest(history, 30, 3)

    # Assert
    assert result is None


def test_backtest_returns_none_for_an_empty_history() -> None:
    # Arrange / Act
    result = backtest([], 7, 3)

    # Assert
    assert result is None


def test_backtest_wape_and_naive_wape_are_none_when_the_tested_actuals_sum_to_zero() -> None:
    # Arrange
    history = _history([0] * 12)

    # Act
    result = backtest(history, 2, 1)

    # Assert
    assert result is not None
    assert result.folds == 1
    assert result.mae_minor == pytest.approx(0.0)
    assert result.wape is None
    assert result.naive_wape is None


def test_backtest_naive_baseline_is_the_last_training_value_and_the_model_is_forecast_series() -> None:
    # Arrange: linear trend 0, 100, ..., 1100; the last day (1100) is tested from the 11 points before it
    history = _history([100 * i for i in range(12)])
    model_forecast = forecast_series(history[:11], 1)[0].expected_minor

    # Act
    result = backtest(history, 1, 1)

    # Assert: naive predicts 1000 (error 100 of 1100)
    assert result is not None
    assert result.folds == 1
    assert result.naive_wape == pytest.approx(100 / 1100)
    assert result.mae_minor == pytest.approx(abs(1100 - model_forecast), abs=0.5)
    assert result.wape == pytest.approx(abs(1100 - model_forecast) / 1100, abs=5e-4)


# --- merchant_forecast (DB) ------------------------------------------------------------------------------


def _payment(
    n: int, merchant_id: uuid.UUID, at: datetime, amount_minor: int, currency: str, *, declined: bool = False
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10**9 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin="backfill",
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="do_not_honor" if declined else None,
        card_bin="424242",
        card_last4="4242",
    )


def _utc(month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=UTC)


@pytest.fixture
def forecast_payments(session_factory: sessionmaker[Session]) -> None:
    events = [
        _payment(1, MERCHANT_A, _utc(10, 27), 300, "USD"),
        _payment(2, MERCHANT_A, _utc(10, 26, 23, 59, 59), 4242, "USD"),
        _payment(3, MERCHANT_A, _utc(11, 1, 10), 2000, "USD"),
        _payment(4, MERCHANT_A, _utc(11, 2, 9), 1000, "USD"),
        _payment(5, MERCHANT_A, _utc(11, 2, 23, 59, 59), 500, "USD"),
        _payment(6, MERCHANT_A, _utc(11, 2, 10), 9999, "USD", declined=True),
        _payment(7, MERCHANT_A, _utc(11, 3, 8), 777777, "USD"),
        _payment(8, MERCHANT_A, _utc(11, 2, 12), 700, "EUR"),
        _payment(9, MERCHANT_A, _utc(11, 1, 12), 800, "GBP", declined=True),
        _payment(10, MERCHANT_A, _utc(10, 26, 12), 600, "JPY"),
        _payment(11, MERCHANT_B, _utc(11, 2, 9), 123456, "USD"),
        _payment(12, MERCHANT_B, _utc(11, 2, 9), 55, "CHF"),
    ]
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)


def test_merchant_forecast_reads_own_succeeded_full_days_per_currency_sorted_by_currency(
    session_factory: sessionmaker[Session], forecast_payments: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        result = merchant_forecast(session, scope, TEST_NOW, 3, history_days=7)

    # Assert: window 2026-10-27 .. 2026-11-02; GBP (declined only) and JPY (outside the window) are absent
    assert [c.currency for c in result] == ["EUR", "USD"]
    eur, usd = result
    assert [p.day for p in usd.history] == [date(2026, 10, 27) + timedelta(days=i) for i in range(7)]
    assert [p.amount_minor for p in usd.history] == [300, 0, 0, 0, 0, 2000, 1500]
    assert [p.amount_minor for p in eur.history] == [0, 0, 0, 0, 0, 0, 700]


def test_merchant_forecast_forecast_follows_forecast_series_of_the_filled_history(
    session_factory: sessionmaker[Session], forecast_payments: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        result = merchant_forecast(session, scope, TEST_NOW, 3, history_days=7)

    # Assert
    usd = next(c for c in result if c.currency == "USD")
    expected = forecast_series(list(usd.history), 3, alpha=usd.alpha)
    assert list(usd.forecast) == expected
    assert usd.alpha == pytest.approx(fit_alpha([p.amount_minor for p in usd.history]))
    assert [p.day for p in usd.forecast] == [date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]
    assert usd.backtest is None


def test_merchant_forecast_default_history_is_28_full_days_and_includes_the_backtest(
    session_factory: sessionmaker[Session], forecast_payments: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_A)

    # Act
    with session_factory() as session:
        result = merchant_forecast(session, scope, TEST_NOW, 3)

    # Assert: window 2026-10-06 .. 2026-11-02 now contains the JPY and the 10-26 USD payments
    assert [c.currency for c in result] == ["EUR", "JPY", "USD"]
    for currency_forecast in result:
        assert len(currency_forecast.history) == 28
        assert currency_forecast.history[0].day == date(2026, 10, 6)
        assert currency_forecast.history[-1].day == date(2026, 11, 2)
        assert currency_forecast.backtest == backtest(list(currency_forecast.history), 3, 3)
    usd = next(c for c in result if c.currency == "USD")
    assert usd.history[20] == DailyPoint(day=date(2026, 10, 26), amount_minor=4242)
    assert usd.history[-1] == DailyPoint(day=date(2026, 11, 2), amount_minor=1500)
    assert usd.backtest is not None
    assert (usd.backtest.horizon_days, usd.backtest.folds) == (3, 3)


def test_merchant_forecast_for_another_merchant_never_sees_the_first_merchants_payments(
    session_factory: sessionmaker[Session], forecast_payments: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT_B)

    # Act
    with session_factory() as session:
        result = merchant_forecast(session, scope, TEST_NOW, 2, history_days=7)

    # Assert
    assert [c.currency for c in result] == ["CHF", "USD"]
    usd = next(c for c in result if c.currency == "USD")
    assert [p.amount_minor for p in usd.history] == [0, 0, 0, 0, 0, 0, 123456]


def test_merchant_forecast_for_a_merchant_without_payments_is_empty(
    session_factory: sessionmaker[Session], forecast_payments: None
) -> None:
    # Arrange
    scope = Scope(merchant_id=uuid.UUID(int=999))

    # Act
    with session_factory() as session:
        result = merchant_forecast(session, scope, TEST_NOW, 7)

    # Assert
    assert result == []


def test_merchant_forecast_excludes_today_even_when_it_is_the_only_day_with_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    today_payment = _payment(1, MERCHANT_A, _utc(11, 3, 11, 59), 5000, "USD")
    with session_factory.begin() as session:
        apply_payment_event(session, lambda: TEST_NOW, today_payment)

    # Act
    with session_factory() as session:
        result = merchant_forecast(session, Scope(merchant_id=MERCHANT_A), TEST_NOW, 7)

    # Assert
    assert result == []
