"""Tests for ops_ai.analytics.anomaly, written from docs/specs/analytics.md section 3."""

import math
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from contracts.events import EventOrigin, PaymentEventV1
from ops_ai.analytics.anomaly import (
    EWMA_ALPHA,
    LOOKBACK_BUCKETS,
    MIN_ATTEMPTS,
    MIN_HISTORY_BUCKETS,
    Z_THRESHOLD,
    BucketStats,
    bucket_start,
    detect,
    ewma_zscore,
    list_alerts,
    observe_payment,
)
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
MERCHANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b2")
JST = timezone(timedelta(hours=9))
BUCKET_ZERO = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
CLOCK_A = TEST_NOW + timedelta(minutes=10)
CLOCK_A_LATER = TEST_NOW + timedelta(minutes=11)
CLOCK_B = TEST_NOW + timedelta(minutes=12)
HISTORY_BUCKETS = 8
HISTORY_PER_BUCKET = 12
HISTORY_RATE = 1 / HISTORY_PER_BUCKET


# --- constants -------------------------------------------------------------------------------------------


def test_module_constants_are_the_specified_ones() -> None:
    # Arrange / Act
    constants = (LOOKBACK_BUCKETS, EWMA_ALPHA, Z_THRESHOLD, MIN_ATTEMPTS, MIN_HISTORY_BUCKETS)

    # Assert
    assert constants == (24, 0.3, 3.0, 10, 4)


# --- bucket_start ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (datetime(2026, 11, 3, 12, 7, 33, tzinfo=UTC), datetime(2026, 11, 3, 12, 0, tzinfo=UTC)),
        (datetime(2026, 11, 3, 12, 0, 0, tzinfo=UTC), datetime(2026, 11, 3, 12, 0, tzinfo=UTC)),
        (datetime(2026, 11, 3, 12, 14, 59, 999999, tzinfo=UTC), datetime(2026, 11, 3, 12, 0, tzinfo=UTC)),
        (datetime(2026, 11, 3, 12, 15, 0, tzinfo=UTC), datetime(2026, 11, 3, 12, 15, tzinfo=UTC)),
        (datetime(2026, 11, 3, 23, 59, 59, tzinfo=UTC), datetime(2026, 11, 3, 23, 45, tzinfo=UTC)),
        (datetime(2026, 11, 3, 21, 7, tzinfo=JST), datetime(2026, 11, 3, 12, 0, tzinfo=UTC)),
    ],
    ids=["mid_bucket", "on_boundary", "last_microsecond", "next_bucket", "end_of_day", "offset_timezone"],
)
def test_bucket_start_floors_to_the_15_minute_epoch_aligned_utc_bucket(
    at: datetime, expected: datetime
) -> None:
    # Arrange / Act
    start = bucket_start(at)

    # Assert
    assert start == expected
    assert start.utcoffset() == timedelta(0)


# --- ewma_zscore -----------------------------------------------------------------------------------------


def test_ewma_zscore_follows_the_spec_recurrence_with_the_default_alpha() -> None:
    # Arrange: m 0.1 -> 0.16 -> 0.172 -> 0.1804 and v 0 -> 0.0084 -> 0.006216 -> 0.00451584 (sd 0.0672)
    history = [0.1, 0.3, 0.2, 0.2]

    # Act
    z = ewma_zscore(history, 0.5)

    # Assert
    assert z == pytest.approx((0.5 - 0.1804) / 0.0672)


def test_ewma_zscore_with_an_explicit_alpha() -> None:
    # Arrange: alpha 0.5 -> m 0 -> 5 -> 7.5 -> 8.75 and v 0 -> 25 -> 18.75 -> 10.9375
    history = [0.0, 10.0, 10.0, 10.0]

    # Act
    z = ewma_zscore(history, 15.0, alpha=0.5)

    # Assert
    assert z == pytest.approx((15.0 - 8.75) / math.sqrt(10.9375))


@pytest.mark.parametrize("length", [0, 1, 3])
def test_ewma_zscore_is_none_below_the_minimum_history(length: int) -> None:
    # Arrange
    history = [0.1] * length

    # Act
    z = ewma_zscore(history, 0.9)

    # Assert
    assert z is None


def test_ewma_zscore_scores_exactly_the_minimum_history() -> None:
    # Arrange
    history = [5.0] * MIN_HISTORY_BUCKETS

    # Act
    z = ewma_zscore(history, 5.0)

    # Assert
    assert z == 0.0


def test_ewma_zscore_min_std_floors_the_standard_deviation() -> None:
    # Arrange: the EWMA standard deviation is 0.0672
    history = [0.1, 0.3, 0.2, 0.2]

    # Act
    floored = ewma_zscore(history, 0.5, min_std=0.1)
    not_binding = ewma_zscore(history, 0.5, min_std=0.05)

    # Assert
    assert floored == pytest.approx((0.5 - 0.1804) / 0.1)
    assert not_binding == pytest.approx((0.5 - 0.1804) / 0.0672)


def test_ewma_zscore_of_a_flat_history_without_a_floor_is_infinite_above_and_zero_at_the_mean() -> None:
    # Arrange
    history = [5.0, 5.0, 5.0, 5.0]

    # Act
    above = ewma_zscore(history, 6.0)
    equal = ewma_zscore(history, 5.0)

    # Assert
    assert above == math.inf
    assert equal == 0.0


def test_ewma_zscore_of_a_flat_history_with_a_floor_divides_by_the_floor() -> None:
    # Arrange
    history = [5.0, 5.0, 5.0, 5.0]

    # Act
    z = ewma_zscore(history, 6.0, min_std=0.5)

    # Assert
    assert z == pytest.approx(2.0)


# --- detect ----------------------------------------------------------------------------------------------


def _stats(index: int, attempts: int, declined: int, by_code: dict[str, int]) -> BucketStats:
    start = BUCKET_ZERO + timedelta(minutes=15 * index)
    return BucketStats(start=start, attempts=attempts, declined=declined, by_code=by_code)


def _steady_history(count: int = 6) -> list[BucketStats]:
    return [_stats(i, 20, 1, {"do_not_honor": 1}) for i in range(count)]


def test_detect_returns_nothing_for_a_bucket_that_looks_like_its_history() -> None:
    # Arrange
    current = _stats(6, 20, 1, {"do_not_honor": 1})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    assert anomalies == []


def test_detect_flags_a_failure_rate_spike_with_observed_expected_and_zscore() -> None:
    # Arrange: history rate 0.05 (floored sd 0.02), current rate 0.5 -> z 22.5
    current = _stats(6, 20, 10, {"do_not_honor": 10})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    failure = next(a for a in anomalies if a.metric == "failure_rate")
    assert failure.detail is None
    assert failure.observed == pytest.approx(0.5)
    assert failure.expected == pytest.approx(0.05)
    assert failure.zscore == pytest.approx(22.5)


def test_detect_does_not_flag_failure_rate_below_the_minimum_attempts() -> None:
    # Arrange: 9 attempts, all declined
    current = _stats(6, MIN_ATTEMPTS - 1, MIN_ATTEMPTS - 1, {"do_not_honor": MIN_ATTEMPTS - 1})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    assert [a for a in anomalies if a.metric == "failure_rate"] == []


def test_detect_flags_failure_rate_at_exactly_the_minimum_attempts() -> None:
    # Arrange
    current = _stats(6, MIN_ATTEMPTS, MIN_ATTEMPTS, {"do_not_honor": MIN_ATTEMPTS})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    assert [a.metric for a in anomalies if a.metric == "failure_rate"] == ["failure_rate"]


def test_detect_flags_a_volume_spike_only() -> None:
    # Arrange: 40 attempts against a steady 20 (floored sd 1.0), same failure mix
    current = _stats(6, 40, 2, {"do_not_honor": 2})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    assert len(anomalies) == 1
    assert anomalies[0].metric == "volume"
    assert anomalies[0].detail is None
    assert anomalies[0].observed == pytest.approx(40.0)
    assert anomalies[0].expected == pytest.approx(20.0)
    assert anomalies[0].zscore == pytest.approx(20.0)


def test_detect_does_not_flag_a_volume_drop() -> None:
    # Arrange
    current = _stats(6, 2, 0, {})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    assert anomalies == []


def test_detect_decline_mix_reports_the_code_with_the_highest_zscore_in_detail() -> None:
    # Arrange: do_not_honor share 0.3 (z 12.5), insufficient_funds share 0.2 (z 10, history share 0)
    current = _stats(6, 20, 10, {"do_not_honor": 6, "insufficient_funds": 4})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    mix = [a for a in anomalies if a.metric == "decline_mix"]
    assert len(mix) == 1
    assert mix[0].detail == "do_not_honor"
    assert mix[0].observed == pytest.approx(0.3)
    assert mix[0].expected == pytest.approx(0.05)
    assert mix[0].zscore == pytest.approx(12.5)


def test_detect_decline_mix_flags_a_code_that_never_appeared_in_the_history() -> None:
    # Arrange
    current = _stats(6, 20, 8, {"issuer_unavailable": 8})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    mix = next(a for a in anomalies if a.metric == "decline_mix")
    assert mix.detail == "issuer_unavailable"
    assert mix.observed == pytest.approx(0.4)
    assert mix.expected == pytest.approx(0.0)
    assert mix.zscore == pytest.approx(20.0)


def test_detect_orders_results_failure_rate_then_volume_then_decline_mix() -> None:
    # Arrange: 40 attempts of which 30 declined with one new code
    current = _stats(6, 40, 30, {"issuer_unavailable": 30})

    # Act
    anomalies = detect(_steady_history(), current)

    # Assert
    assert [a.metric for a in anomalies] == ["failure_rate", "volume", "decline_mix"]
    assert all(a.zscore >= Z_THRESHOLD for a in anomalies)


def test_detect_skips_history_buckets_without_attempts_for_the_rate_metrics() -> None:
    # Arrange: empty buckets interleaved with the steady ones; counting them as rate 0 would change the sd
    history = []
    for i in range(9):
        history.append(_stats(i, 20, 1, {"do_not_honor": 1}) if i % 2 == 0 else _stats(i, 0, 0, {}))
    current = _stats(9, 20, 10, {"do_not_honor": 10})

    # Act
    anomalies = detect(history, current)

    # Assert
    failure = next(a for a in anomalies if a.metric == "failure_rate")
    assert failure.expected == pytest.approx(0.05)
    assert failure.zscore == pytest.approx(22.5)


def test_detect_cannot_score_a_rate_without_enough_non_empty_history_buckets() -> None:
    # Arrange: 3 usable buckets (below MIN_HISTORY_BUCKETS) and 5 empty ones
    usable = (1, 3, 5)
    history = [
        _stats(i, 20, 1, {"do_not_honor": 1}) if i in usable else _stats(i, 0, 0, {}) for i in range(8)
    ]
    current = _stats(8, 20, 20, {"do_not_honor": 20})

    # Act
    anomalies = detect(history, current)

    # Assert
    assert [a for a in anomalies if a.metric == "failure_rate"] == []


def test_detect_with_too_little_history_flags_nothing() -> None:
    # Arrange
    current = _stats(3, 100, 100, {"do_not_honor": 100})

    # Act
    anomalies = detect(_steady_history(MIN_HISTORY_BUCKETS - 1), current)

    # Assert
    assert anomalies == []


# --- observe_payment / list_alerts (DB) ------------------------------------------------------------------


def _event(
    n: int,
    merchant_id: uuid.UUID,
    at: datetime,
    *,
    declined: bool,
    decline_code: str | None = None,
    origin: EventOrigin = "live",
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10**9 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin=origin,
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code=decline_code if declined else None,
        card_bin="424242",
        card_last4="4242",
    )


def _history_events(merchant_id: uuid.UUID, base: int) -> list[PaymentEventV1]:
    """Eight steady buckets before TEST_NOW: 12 payments each, the first declined (do_not_honor)."""
    first_bucket = TEST_NOW - timedelta(minutes=15 * HISTORY_BUCKETS)
    return [
        _event(
            base + bucket * HISTORY_PER_BUCKET + minute,
            merchant_id,
            first_bucket + timedelta(minutes=15 * bucket + minute),
            declined=minute == 0,
            decline_code="do_not_honor",
            origin="backfill",
        )
        for bucket in range(HISTORY_BUCKETS)
        for minute in range(HISTORY_PER_BUCKET)
    ]


def _burst(merchant_id: uuid.UUID, base: int, count: int, origin: EventOrigin) -> list[PaymentEventV1]:
    """`count` declines (issuer_unavailable) at TEST_NOW + 1 .. count minutes, all in the 12:00 bucket."""
    return [
        _event(
            base + k,
            merchant_id,
            TEST_NOW + timedelta(minutes=k + 1),
            declined=True,
            decline_code="issuer_unavailable",
            origin=origin,
        )
        for k in range(count)
    ]


def _apply(session_factory: sessionmaker[Session], events: list[PaymentEventV1], at: datetime) -> None:
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: at, event)


def _rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        result = session.execute(
            text(
                "SELECT alert_id, alert_key, merchant_id, metric, detail, bucket_start, observed, expected, "
                "zscore, attempts, created_at, updated_at FROM ops_ai.anomaly_alerts "
                "ORDER BY merchant_id, metric, detail NULLS FIRST"
            )
        )
        return [dict(row) for row in result.mappings()]


def _row(rows: list[dict[str, object]], merchant_id: uuid.UUID, metric: str) -> dict[str, object]:
    [row] = [r for r in rows if r["merchant_id"] == merchant_id and r["metric"] == metric]
    return row


def test_apply_payment_event_with_live_events_triggers_the_anomaly_hook(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)

    # Act
    _apply(session_factory, _burst(MERCHANT_A, 5_000, 10, "live"), CLOCK_A)

    # Assert
    rows = _rows(session_factory)
    assert {(str(r["metric"]), r["detail"]) for r in rows} == {
        ("decline_mix", "issuer_unavailable"),
        ("failure_rate", None),
    }


def test_a_live_burst_after_steady_history_creates_exactly_one_failure_rate_row_and_one_decline_mix_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)

    # Act
    _apply(session_factory, _burst(MERCHANT_A, 5_000, 10, "live"), CLOCK_A)

    # Assert: history rate 1/12 (floored sd 0.02), burst rate 1.0; the new code has history share 0
    rows = _rows(session_factory)
    assert len(rows) == 2
    failure = _row(rows, MERCHANT_A, "failure_rate")
    assert failure["detail"] is None
    assert failure["bucket_start"] == TEST_NOW
    assert failure["attempts"] == 10
    assert failure["observed"] == pytest.approx(1.0)
    assert failure["expected"] == pytest.approx(HISTORY_RATE)
    assert failure["zscore"] == pytest.approx((1.0 - HISTORY_RATE) / 0.02)
    assert (failure["created_at"], failure["updated_at"]) == (CLOCK_A, CLOCK_A)
    mix = _row(rows, MERCHANT_A, "decline_mix")
    assert mix["detail"] == "issuer_unavailable"
    assert mix["observed"] == pytest.approx(1.0)
    assert mix["expected"] == pytest.approx(0.0)
    assert mix["zscore"] == pytest.approx(50.0)
    assert mix["alert_key"] == f"decline_mix:{MERCHANT_A}:2026-11-03T12:00:issuer_unavailable"
    assert failure["alert_key"] == f"failure_rate:{MERCHANT_A}:2026-11-03T12:00:"


def test_failure_rate_alert_appears_only_once_the_current_bucket_has_the_minimum_attempts(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)
    burst = _burst(MERCHANT_A, 5_000, MIN_ATTEMPTS, "live")

    # Act
    _apply(session_factory, burst[: MIN_ATTEMPTS - 1], CLOCK_A)
    metrics_before = {r["metric"] for r in _rows(session_factory)}
    _apply(session_factory, burst[MIN_ATTEMPTS - 1 :], CLOCK_A)
    metrics_after = {r["metric"] for r in _rows(session_factory)}

    # Assert
    assert "failure_rate" not in metrics_before
    assert "failure_rate" in metrics_after


def test_a_second_live_event_in_the_same_bucket_updates_the_same_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)
    _apply(session_factory, _burst(MERCHANT_A, 5_000, 10, "live"), CLOCK_A)
    before = _rows(session_factory)
    succeeded = _event(6_000, MERCHANT_A, TEST_NOW + timedelta(minutes=11), declined=False)

    # Act
    _apply(session_factory, [succeeded], CLOCK_A_LATER)

    # Assert: 10 declines of 11 attempts, same alert ids, creation time kept, update time moved
    after = _rows(session_factory)
    assert len(after) == 2
    for metric in ("failure_rate", "decline_mix"):
        old, new = _row(before, MERCHANT_A, metric), _row(after, MERCHANT_A, metric)
        assert new["alert_id"] == old["alert_id"]
        assert new["created_at"] == CLOCK_A
        assert new["updated_at"] == CLOCK_A_LATER
        assert new["attempts"] == 11
        assert new["observed"] == pytest.approx(10 / 11)
    failure = _row(after, MERCHANT_A, "failure_rate")
    assert failure["zscore"] == pytest.approx((10 / 11 - HISTORY_RATE) / 0.02)


def test_a_calm_live_event_after_steady_history_creates_no_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)
    calm = _event(6_000, MERCHANT_A, TEST_NOW + timedelta(minutes=1), declined=False)

    # Act
    _apply(session_factory, [calm], CLOCK_A)

    # Assert
    assert _rows(session_factory) == []


def test_observe_payment_for_a_backfill_event_returns_nothing_and_writes_no_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the anomalous bucket is fully projected (backfill events skip the hook in the consumer)
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)
    burst = _burst(MERCHANT_A, 5_000, 10, "backfill")
    _apply(session_factory, burst, CLOCK_A)

    # Act
    with session_factory.begin() as session:
        created = observe_payment(session, lambda: CLOCK_A, burst[-1])

    # Assert
    assert created == []
    assert _rows(session_factory) == []


def test_observe_payment_for_the_same_event_as_live_creates_the_alerts(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _history_events(MERCHANT_A, 1_000), CLOCK_A)
    burst = _burst(MERCHANT_A, 5_000, 10, "backfill")
    _apply(session_factory, burst, CLOCK_A)
    live_copy = burst[-1].model_copy(update={"origin": "live"})

    # Act
    with session_factory.begin() as session:
        created = observe_payment(session, lambda: CLOCK_A, live_copy)

    # Assert
    rows = _rows(session_factory)
    assert len(created) == 2
    assert set(created) == {r["alert_id"] for r in rows}


def _seed_two_merchants(session_factory: sessionmaker[Session]) -> None:
    history = _history_events(MERCHANT_A, 1_000) + _history_events(MERCHANT_B, 20_000)
    _apply(session_factory, history, CLOCK_A)
    _apply(session_factory, _burst(MERCHANT_A, 5_000, 10, "live"), CLOCK_A)
    _apply(session_factory, _burst(MERCHANT_B, 25_000, 10, "live"), CLOCK_B)


def test_alerts_of_one_merchant_never_depend_on_another_merchants_traffic(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    _seed_two_merchants(session_factory)

    # Assert: each merchant has its own two rows with its own attempts
    rows = _rows(session_factory)
    assert {(str(r["merchant_id"]), str(r["metric"]), r["attempts"]) for r in rows} == {
        (str(MERCHANT_A), "decline_mix", 10),
        (str(MERCHANT_A), "failure_rate", 10),
        (str(MERCHANT_B), "decline_mix", 10),
        (str(MERCHANT_B), "failure_rate", 10),
    }
    assert len(rows) == 4


def test_list_alerts_with_a_merchant_scope_returns_only_that_merchants_alerts(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_two_merchants(session_factory)

    # Act
    with session_factory() as session:
        views = list_alerts(session, Scope(merchant_id=MERCHANT_A), None, 50)

    # Assert
    assert {v.merchant_id for v in views} == {MERCHANT_A}
    assert sorted(v.metric for v in views) == ["decline_mix", "failure_rate"]


def test_list_alerts_without_a_scope_lists_every_merchant_newest_update_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_two_merchants(session_factory)

    # Act
    with session_factory() as session:
        views = list_alerts(session, None, None, 50)

    # Assert
    assert [v.merchant_id for v in views] == [MERCHANT_B, MERCHANT_B, MERCHANT_A, MERCHANT_A]
    assert [v.updated_at for v in views] == [CLOCK_B, CLOCK_B, CLOCK_A, CLOCK_A]


def test_list_alerts_since_keeps_only_alerts_updated_strictly_after(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_two_merchants(session_factory)

    # Act
    with session_factory() as session:
        after_a = list_alerts(session, None, CLOCK_A, 50)
        before_a = list_alerts(session, None, CLOCK_A - timedelta(seconds=1), 50)
        after_b = list_alerts(session, None, CLOCK_B, 50)

    # Assert
    assert {v.merchant_id for v in after_a} == {MERCHANT_B}
    assert len(before_a) == 4
    assert after_b == []


def test_list_alerts_limit_caps_the_number_of_items(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_two_merchants(session_factory)

    # Act
    with session_factory() as session:
        views = list_alerts(session, None, None, 3)

    # Assert
    assert [v.merchant_id for v in views] == [MERCHANT_B, MERCHANT_B, MERCHANT_A]


def test_list_alerts_view_carries_the_stored_values(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_two_merchants(session_factory)
    stored = _row(_rows(session_factory), MERCHANT_A, "failure_rate")

    # Act
    with session_factory() as session:
        views = list_alerts(session, Scope(merchant_id=MERCHANT_A), None, 50)

    # Assert
    view = next(v for v in views if v.metric == "failure_rate")
    assert view.alert_id == stored["alert_id"]
    assert view.detail is None
    assert view.bucket_start == TEST_NOW
    assert view.attempts == 10
    assert view.observed == pytest.approx(1.0)
    assert view.expected == pytest.approx(HISTORY_RATE)
    assert view.zscore == pytest.approx((1.0 - HISTORY_RATE) / 0.02)
    assert (view.created_at, view.updated_at) == (CLOCK_A, CLOCK_A)


# --- the anomaly hook never blocks ingest -----------------------------------------------------------------


@pytest.mark.parametrize(
    "occurred_at",
    [datetime(9999, 12, 31, 23, 59, tzinfo=UTC), datetime(1, 1, 1, 0, 5, tzinfo=UTC)],
    ids=["max_datetime", "min_datetime"],
)
def test_apply_payment_event_with_a_live_event_at_the_datetime_limits_stores_the_fact_and_writes_no_alert(
    session_factory: sessionmaker[Session], occurred_at: datetime
) -> None:
    # Arrange
    event = _event(7_000, MERCHANT_A, occurred_at, declined=True, decline_code="do_not_honor")

    # Act
    with session_factory.begin() as session:
        applied = apply_payment_event(session, lambda: CLOCK_A, event)

    # Assert
    with session_factory() as session:
        stored = (
            session.execute(
                text("SELECT occurred_at FROM ops_ai.payment_facts WHERE payment_id = :payment_id"),
                {"payment_id": event.payment_id},
            )
            .scalars()
            .all()
        )
    assert applied is True
    assert stored == [occurred_at]
    assert _rows(session_factory) == []
