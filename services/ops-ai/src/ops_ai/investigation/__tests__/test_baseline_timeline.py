"""Tests for compare_to_baseline and change_timeline (docs/specs/failure-tools.md section 6)."""

import uuid
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.tools import (
    MAX_TIMELINE_BUCKETS,
    MIN_ELEVATED_FAILURES,
    BaselineComparison,
    ChangeEvent,
    ChangeTimeline,
    TimelineBucket,
    aggregate_failures,
    change_timeline,
    compare_to_baseline,
)
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

START = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
END = START + timedelta(hours=6)
BASELINE_START = START - timedelta(days=7)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
INCIDENT_STARTED = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)


def _fact(
    session: Session,
    occurred_at: datetime,
    *,
    state: str = "declined",
    card_bin: str | None = "400000",
    decline_code: str | None = None,
    merchant_id: uuid.UUID = MERCHANT,
    method: str = "card",
    no_decline_code: bool = False,
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    if decline_code is None and state in ("declined", "failed") and not no_decline_code:
        decline_code = "card_declined"
    session.add(
        PaymentFact(
            payment_id=payment_id,
            merchant_id=merchant_id,
            amount_minor=1500,
            currency="USD",
            method=method,
            state=state,
            provider="acq_a",
            decline_code=decline_code,
            card_bin=card_bin,
            entity_version=1,
            occurred_at=occurred_at,
        )
    )
    return payment_id


def _incident(
    session: Session,
    incident_id: str,
    merchant_id: uuid.UUID | None,
    started_at: datetime,
) -> None:
    session.execute(
        text(
            "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
            "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
            "(:incident_id, :merchant_id, 'Issuer outage', 'Issuer timeouts', 'Rerouted', 'notes', "
            ":started_at, NULL, false)"
        ),
        {"incident_id": incident_id, "merchant_id": merchant_id, "started_at": started_at},
    )


def _seed_rates(
    session_factory: sessionmaker[Session],
    *,
    window_failed: int,
    window_ok: int,
    baseline_failed: int,
    baseline_ok: int,
    merchant_id: uuid.UUID = MERCHANT,
) -> None:
    """One record per minute from the start of the window and from one minute into the baseline."""
    with session_factory.begin() as session:
        for index in range(window_failed + window_ok):
            _fact(
                session,
                START + timedelta(minutes=index),
                state="declined" if index < window_failed else "succeeded",
                merchant_id=merchant_id,
            )
        for index in range(baseline_failed + baseline_ok):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index < baseline_failed else "succeeded",
                merchant_id=merchant_id,
            )


def _compare(session_factory: sessionmaker[Session], **kwargs: int) -> BaselineComparison:
    with session_factory() as session:
        return compare_to_baseline(session, SCOPE, START, END, **kwargs)


def _timeline(
    session_factory: sessionmaker[Session],
    start: datetime = START,
    end: datetime = END,
    **kwargs: int,
) -> ChangeTimeline:
    with session_factory() as session:
        return change_timeline(session, SCOPE, start, end, **kwargs)


def _keys(timeline: ChangeTimeline) -> list[tuple[datetime, str, str]]:
    return [(change.at, change.kind, change.subject) for change in timeline.changes]


def _kind_keys(timeline: ChangeTimeline, kind: str) -> list[tuple[datetime, str]]:
    return [(change.at, change.subject) for change in timeline.changes if change.kind == kind]


# --- compare_to_baseline: shape --------------------------------------------------------------------------


def test_min_elevated_failures_is_ten() -> None:
    # Arrange / Act
    floor = MIN_ELEVATED_FAILURES

    # Assert
    assert floor == 10


def test_compare_to_baseline_aggregates_the_window_and_the_seven_days_before_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_rates(session_factory, window_failed=4, window_ok=4, baseline_failed=1, baseline_ok=7)

    # Act
    result = _compare(session_factory)

    # Assert
    with session_factory() as session:
        expected_window = aggregate_failures(session, SCOPE, START, END)
        expected_baseline = aggregate_failures(session, SCOPE, BASELINE_START, START)
    assert result.window == expected_window
    assert result.baseline == expected_baseline
    assert (result.window.total, result.window.failed) == (8, 4)
    assert (result.baseline.total, result.baseline.failed) == (8, 1)


def test_compare_to_baseline_ratio_is_the_window_rate_over_the_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 4/8 = 0.5 against 1/8 = 0.125, both exact in binary floating point
    _seed_rates(session_factory, window_failed=4, window_ok=4, baseline_failed=1, baseline_ok=7)

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.ratio == 4.0


def test_compare_to_baseline_uses_the_given_number_of_baseline_days(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=1), state="declined")
        _fact(session, START - timedelta(days=2), state="declined")
        _fact(session, START - timedelta(hours=1), state="succeeded")
        _fact(session, START - timedelta(hours=30), state="succeeded")

    # Act
    one_day = _compare(session_factory, baseline_days=1)
    three_days = _compare(session_factory, baseline_days=3)

    # Assert
    assert (one_day.baseline.start, one_day.baseline.end) == (START - timedelta(days=1), START)
    assert (one_day.baseline.total, one_day.baseline.failed) == (1, 0)
    assert (three_days.baseline.start, three_days.baseline.end) == (START - timedelta(days=3), START)
    assert (three_days.baseline.total, three_days.baseline.failed) == (3, 1)


def test_compare_to_baseline_ratio_is_none_when_the_baseline_has_payments_but_no_failures(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_rates(session_factory, window_failed=4, window_ok=4, baseline_failed=0, baseline_ok=10)

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.ratio is None
    assert result.baseline.rate == 0.0


def test_compare_to_baseline_ratio_is_none_when_the_baseline_has_no_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_rates(session_factory, window_failed=4, window_ok=4, baseline_failed=0, baseline_ok=0)

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.ratio is None
    assert (result.baseline.total, result.baseline.failed) == (0, 0)


def test_compare_to_baseline_over_empty_data_is_not_elevated_and_has_no_ratio(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: nothing seeded

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.ratio is None
    assert result.elevated is False
    assert (result.window.total, result.baseline.total) == (0, 0)


def test_compare_to_baseline_never_counts_another_merchants_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_rates(session_factory, window_failed=2, window_ok=2, baseline_failed=1, baseline_ok=3)
    _seed_rates(
        session_factory,
        window_failed=30,
        window_ok=0,
        baseline_failed=0,
        baseline_ok=50,
        merchant_id=OTHER_MERCHANT,
    )

    # Act
    result = _compare(session_factory)

    # Assert
    assert (result.window.total, result.window.failed) == (4, 2)
    assert (result.baseline.total, result.baseline.failed) == (4, 1)
    assert result.elevated is False


# --- compare_to_baseline: elevated -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("window_failed", "window_ok", "baseline_failed", "baseline_ok", "expected"),
    [
        (10, 10, 5, 15, True),
        (10, 10, 6, 14, False),
        (10, 10, 4, 16, True),
        (10, 0, 1, 9, True),
        (9, 0, 1, 9, False),
        (11, 0, 1, 9, True),
        (10, 0, 0, 10, True),
        (10, 0, 0, 0, True),
        (9, 0, 0, 10, False),
        (10, 20, 1, 5, True),
        (10, 20, 1, 4, False),
        (10, 10, 10, 10, False),
    ],
    ids=[
        "ten_failures_rate_exactly_twice_the_baseline",
        "ten_failures_rate_just_under_twice_the_baseline",
        "ten_failures_rate_above_twice_the_baseline",
        "exactly_ten_failures_all_failing",
        "nine_failures_is_below_the_floor",
        "eleven_failures",
        "ten_failures_against_a_baseline_without_failures",
        "ten_failures_against_a_baseline_without_payments",
        "nine_failures_against_a_baseline_without_failures",
        "twice_the_baseline_with_non_terminating_rates",
        "just_under_twice_the_baseline_with_non_terminating_rates",
        "rate_equal_to_the_baseline",
    ],
)
def test_compare_to_baseline_elevated_needs_ten_failures_and_a_rate_of_at_least_twice_the_baseline(
    session_factory: sessionmaker[Session],
    window_failed: int,
    window_ok: int,
    baseline_failed: int,
    baseline_ok: int,
    expected: bool,
) -> None:
    # Arrange
    _seed_rates(
        session_factory,
        window_failed=window_failed,
        window_ok=window_ok,
        baseline_failed=baseline_failed,
        baseline_ok=baseline_ok,
    )

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.elevated is expected


def test_compare_to_baseline_ratio_is_two_when_the_window_rate_is_exactly_twice_the_baseline(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 10/20 = 0.5 against 5/20 = 0.25
    _seed_rates(session_factory, window_failed=10, window_ok=10, baseline_failed=5, baseline_ok=15)

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.ratio == 2.0
    assert result.elevated is True


def test_compare_to_baseline_nine_failures_are_not_elevated_even_at_a_hundred_times_the_baseline(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 9/9 = 1.0 against 1/100 = 0.01
    _seed_rates(session_factory, window_failed=9, window_ok=0, baseline_failed=1, baseline_ok=99)

    # Act
    result = _compare(session_factory)

    # Assert
    assert result.ratio == 100.0
    assert result.elevated is False


def test_compare_to_baseline_only_failed_states_count_towards_the_ten_failure_floor(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute, state in enumerate(["declined"] * 5 + ["failed"] * 4 + ["unknown", "created", "pending"]):
            _fact(session, START + timedelta(minutes=minute), state=state)
        _fact(session, BASELINE_START + timedelta(minutes=1), state="succeeded")

    # Act
    result = _compare(session_factory)

    # Assert
    assert (result.window.total, result.window.failed) == (12, 9)
    assert result.elevated is False


# --- change_timeline: buckets ----------------------------------------------------------------------------


def test_max_timeline_buckets_is_ninety_six() -> None:
    # Arrange / Act
    cap = MAX_TIMELINE_BUCKETS

    # Assert
    assert cap == 96


def test_change_timeline_covers_the_window_back_to_back_in_hourly_buckets_by_default(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: nothing seeded

    # Act
    result = _timeline(session_factory)

    # Assert
    hours = [START + timedelta(hours=n) for n in range(7)]
    bounds = [(bucket.start, bucket.end) for bucket in result.buckets]
    assert bounds == list(pairwise(hours))
    assert len(result.buckets) == 6


def test_change_timeline_uses_the_given_bucket_length(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    end = START + timedelta(hours=1)

    # Act
    result = _timeline(session_factory, START, end, bucket_minutes=15)

    # Assert
    quarter_hours = [START + timedelta(minutes=15 * n) for n in range(5)]
    assert [(bucket.start, bucket.end) for bucket in result.buckets] == list(pairwise(quarter_hours))


def test_change_timeline_last_bucket_is_shorter_when_the_window_is_not_a_multiple_of_the_bucket_length(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    end = START + timedelta(minutes=90)

    # Act
    result = _timeline(session_factory, START, end, bucket_minutes=60)

    # Assert
    assert [(bucket.start, bucket.end) for bucket in result.buckets] == [
        (START, START + timedelta(hours=1)),
        (START + timedelta(hours=1), end),
    ]


def test_change_timeline_a_window_shorter_than_one_bucket_gives_a_single_bucket_ending_at_the_window_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    end = START + timedelta(minutes=20)

    # Act
    result = _timeline(session_factory, START, end, bucket_minutes=60)

    # Assert
    assert [(bucket.start, bucket.end) for bucket in result.buckets] == [(START, end)]


def test_change_timeline_includes_empty_buckets_with_zero_counts_and_a_zero_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(hours=2, minutes=5), state="succeeded")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [(bucket.total, bucket.failed, bucket.rate) for bucket in result.buckets] == [
        (0, 0, 0.0),
        (0, 0, 0.0),
        (1, 0, 0.0),
        (0, 0, 0.0),
        (0, 0, 0.0),
        (0, 0, 0.0),
    ]


def test_change_timeline_over_empty_data_has_empty_buckets_and_no_changes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: nothing seeded

    # Act
    result = _timeline(session_factory)

    # Assert
    assert result.changes == ()
    assert [bucket.total for bucket in result.buckets] == [0, 0, 0, 0, 0, 0]


def test_change_timeline_counts_totals_failures_and_rate_per_bucket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute, state in [(5, "declined"), (10, "succeeded"), (20, "failed"), (30, "succeeded")]:
            _fact(session, START + timedelta(minutes=minute), state=state)
        for minute, state in [(65, "unknown"), (70, "declined")]:
            _fact(session, START + timedelta(minutes=minute), state=state)

    # Act
    result = _timeline(session_factory, START, START + timedelta(hours=2))

    # Assert
    assert result.buckets == (
        TimelineBucket(start=START, end=START + timedelta(hours=1), total=4, failed=2, rate=2 / 4),
        TimelineBucket(
            start=START + timedelta(hours=1), end=START + timedelta(hours=2), total=2, failed=1, rate=1 / 2
        ),
    )


def test_change_timeline_puts_a_payment_at_a_bucket_start_in_that_bucket_and_excludes_the_window_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    end = START + timedelta(hours=2)
    with session_factory.begin() as session:
        _fact(session, START - timedelta(seconds=1))
        _fact(session, START)
        _fact(session, START + timedelta(hours=1) - timedelta(seconds=1))
        _fact(session, START + timedelta(hours=1))
        _fact(session, end)

    # Act
    result = _timeline(session_factory, START, end)

    # Assert
    assert [(bucket.total, bucket.failed) for bucket in result.buckets] == [(2, 2), (1, 1)]


def test_change_timeline_ignores_payments_that_are_not_card(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=5), method="wallet")
        _fact(session, START + timedelta(minutes=6), method="bank_transfer")
        _fact(session, START + timedelta(minutes=7), method="card", state="succeeded")

    # Act
    result = _timeline(session_factory, START, START + timedelta(hours=1))

    # Assert
    assert [(bucket.total, bucket.failed) for bucket in result.buckets] == [(1, 0)]


def test_change_timeline_never_counts_another_merchants_payments_or_derives_changes_from_them(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=5), state="succeeded")
        for minute in range(10, 20):
            _fact(
                session,
                START + timedelta(minutes=minute),
                merchant_id=OTHER_MERCHANT,
                card_bin="499999",
                decline_code="do_not_honor",
            )

    # Act
    result = _timeline(session_factory, START, START + timedelta(hours=1))

    # Assert
    assert [(bucket.total, bucket.failed) for bucket in result.buckets] == [(1, 0)]
    assert result.changes == ()


def test_change_timeline_accepts_exactly_ninety_six_buckets(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    end = START + timedelta(hours=24)

    # Act
    result = _timeline(session_factory, START, end, bucket_minutes=15)

    # Assert
    assert len(result.buckets) == 96
    assert result.buckets[-1].end == end


def test_change_timeline_rejects_ninety_seven_buckets(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    end = START + timedelta(hours=24, minutes=1)

    # Act / Assert
    with session_factory() as session, pytest.raises(ValueError):
        change_timeline(session, SCOPE, START, end, bucket_minutes=15)


def test_change_timeline_rejects_a_thirty_day_window_of_hourly_buckets(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    end = START + timedelta(days=30)

    # Act / Assert
    with session_factory() as session, pytest.raises(ValueError):
        change_timeline(session, SCOPE, START, end)


def test_change_timeline_rejects_a_window_that_starts_at_its_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act / Assert
    with session_factory() as session, pytest.raises(ValueError):
        change_timeline(session, SCOPE, START, START)


def test_change_timeline_rejects_a_window_that_starts_after_its_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act / Assert
    with session_factory() as session, pytest.raises(ValueError):
        change_timeline(session, SCOPE, END, START)


# --- change_timeline: failure_onset ----------------------------------------------------------------------


def test_failure_onset_is_the_first_bucket_with_three_failures_at_twice_the_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: baseline 1/10; hour 0 has 2 failures (below 3), hour 1 has 3 of 4, hour 2 also qualifies
    with session_factory.begin() as session:
        for index in range(10):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index == 0 else "succeeded",
            )
        for minute in (5, 6):
            _fact(session, START + timedelta(minutes=minute))
        for minute in (65, 66, 67):
            _fact(session, START + timedelta(minutes=minute))
        _fact(session, START + timedelta(minutes=68), state="succeeded")
        for minute in (125, 126, 127):
            _fact(session, START + timedelta(minutes=minute))

    # Act
    result = _timeline(session_factory)

    # Assert
    onsets = [change for change in result.changes if change.kind == "failure_onset"]
    assert [(change.at, change.subject) for change in onsets] == [(START + timedelta(hours=1), "card")]


def test_failure_onset_needs_at_least_three_failures_in_the_bucket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: two failures out of two is a 100 % rate but under the failure floor
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=5))
        _fact(session, START + timedelta(minutes=6))

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "failure_onset"] == []


def test_failure_onset_accepts_exactly_three_failures_when_the_baseline_has_none(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute))

    # Act
    result = _timeline(session_factory)

    # Assert
    onsets = [change for change in result.changes if change.kind == "failure_onset"]
    assert [(change.at, change.subject) for change in onsets] == [(START, "card")]


def test_failure_onset_accepts_a_bucket_rate_of_exactly_twice_the_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: baseline 1/4 = 0.25, bucket 3/6 = 0.5
    with session_factory.begin() as session:
        for index in range(4):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index == 0 else "succeeded",
            )
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute))
        for minute in (8, 9, 10):
            _fact(session, START + timedelta(minutes=minute), state="succeeded")

    # Act
    result = _timeline(session_factory)

    # Assert
    onsets = [change for change in result.changes if change.kind == "failure_onset"]
    assert [(change.at, change.subject) for change in onsets] == [(START, "card")]


def test_failure_onset_is_absent_when_the_bucket_rate_is_just_under_twice_the_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: baseline 1/4 = 0.25, bucket 3/7 is below 0.5
    with session_factory.begin() as session:
        for index in range(4):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index == 0 else "succeeded",
            )
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute))
        for minute in (8, 9, 10, 11):
            _fact(session, START + timedelta(minutes=minute), state="succeeded")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "failure_onset"] == []


def test_failure_onset_detail_is_a_non_empty_sentence(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute))

    # Act
    result = _timeline(session_factory)

    # Assert
    onset = next(change for change in result.changes if change.kind == "failure_onset")
    assert isinstance(onset, ChangeEvent)
    assert onset.detail.strip() != ""


# --- change_timeline: new_decline_code -------------------------------------------------------------------


def test_new_decline_code_is_reported_at_its_first_window_failure_when_the_baseline_never_had_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = START + timedelta(hours=2, minutes=17)
    with session_factory.begin() as session:
        _fact(session, BASELINE_START + timedelta(minutes=1), decline_code="card_declined")
        _fact(session, first + timedelta(minutes=5), decline_code="issuer_unavailable")
        _fact(session, first, decline_code="issuer_unavailable")
        _fact(session, first + timedelta(minutes=9), decline_code="issuer_unavailable")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _kind_keys(result, "new_decline_code") == [(first, "issuer_unavailable")]


def test_new_decline_code_is_not_reported_for_a_code_the_baseline_already_had(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START - timedelta(hours=2), decline_code="card_declined")
        _fact(session, START + timedelta(minutes=5), decline_code="card_declined")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "new_decline_code"] == []


def test_new_decline_code_baseline_starts_inclusively_seven_days_before_the_window(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: "code_in" failed exactly at the baseline start, "code_out" one second before it
    with session_factory.begin() as session:
        _fact(session, BASELINE_START, decline_code="code_in")
        _fact(session, BASELINE_START - timedelta(seconds=1), decline_code="code_out")
        _fact(session, START + timedelta(minutes=1), decline_code="code_in")
        _fact(session, START + timedelta(minutes=2), decline_code="code_out")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _kind_keys(result, "new_decline_code") == [(START + timedelta(minutes=2), "code_out")]


def test_new_decline_code_follows_the_baseline_days_argument(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START - timedelta(days=2), decline_code="do_not_honor")
        _fact(session, START + timedelta(minutes=3), decline_code="do_not_honor")

    # Act
    seven_days = _timeline(session_factory, baseline_days=7)
    one_day = _timeline(session_factory, baseline_days=1)

    # Assert
    assert _kind_keys(seven_days, "new_decline_code") == []
    assert _kind_keys(one_day, "new_decline_code") == [(START + timedelta(minutes=3), "do_not_honor")]


def test_new_decline_code_ignores_failures_without_a_decline_code(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=3), no_decline_code=True)

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "new_decline_code"] == []


def test_new_decline_code_ignores_successful_payments(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=3), state="succeeded")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert result.changes == ()


def test_new_decline_code_reports_each_new_code_once(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=30), decline_code="rate_limited")
        _fact(session, START + timedelta(minutes=10), decline_code="expired_card")
        _fact(session, START + timedelta(minutes=40), decline_code="rate_limited")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _kind_keys(result, "new_decline_code") == [
        (START + timedelta(minutes=10), "expired_card"),
        (START + timedelta(minutes=30), "rate_limited"),
    ]


# --- change_timeline: bin_onset --------------------------------------------------------------------------


def test_bin_onset_is_reported_at_the_first_window_failure_of_a_bin_failing_at_twice_its_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: bin 400000 baseline 1/10 (0.1), window 3/3
    first = START + timedelta(minutes=41)
    with session_factory.begin() as session:
        for index in range(10):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index == 0 else "succeeded",
                card_bin="400000",
            )
        for offset in (9, 0, 3):
            _fact(session, first + timedelta(minutes=offset), card_bin="400000")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _kind_keys(result, "bin_onset") == [(first, "400000")]


def test_bin_onset_needs_at_least_three_window_failures_in_the_bin(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=1), card_bin="411111")
        _fact(session, START + timedelta(minutes=2), card_bin="411111")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "bin_onset"] == []


def test_bin_onset_accepts_a_window_rate_of_exactly_twice_the_bins_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: baseline 1/4 = 0.25, window 3/6 = 0.5
    with session_factory.begin() as session:
        for index in range(4):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index == 0 else "succeeded",
                card_bin="424242",
            )
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute), card_bin="424242")
        for minute in (8, 9, 10):
            _fact(session, START + timedelta(minutes=minute), state="succeeded", card_bin="424242")

    # Act
    result = _timeline(session_factory)

    # Assert
    bin_changes = [change for change in result.changes if change.kind == "bin_onset"]
    assert [(change.at, change.subject) for change in bin_changes] == [
        (START + timedelta(minutes=5), "424242")
    ]


def test_bin_onset_is_absent_when_the_window_rate_is_just_under_twice_the_bins_baseline_rate(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: baseline 1/4 = 0.25, window 3/7 is below 0.5
    with session_factory.begin() as session:
        for index in range(4):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if index == 0 else "succeeded",
                card_bin="424242",
            )
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute), card_bin="424242")
        for minute in (8, 9, 10, 11):
            _fact(session, START + timedelta(minutes=minute), state="succeeded", card_bin="424242")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "bin_onset"] == []


def test_bin_onset_compares_each_bin_with_its_own_baseline_not_the_overall_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: bin 400000 always fails (baseline 5/5, window 3/3), bin 411111 is healthy before (0/5)
    with session_factory.begin() as session:
        for index in range(5):
            _fact(session, BASELINE_START + timedelta(minutes=index + 1), card_bin="400000")
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 10),
                state="succeeded",
                card_bin="411111",
            )
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute), card_bin="400000")
            _fact(session, START + timedelta(minutes=minute + 20), card_bin="411111")

    # Act
    result = _timeline(session_factory)

    # Assert
    bin_changes = [change for change in result.changes if change.kind == "bin_onset"]
    assert [(change.at, change.subject) for change in bin_changes] == [
        (START + timedelta(minutes=25), "411111")
    ]


def test_bin_onset_accepts_a_bin_that_has_no_baseline_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute in (5, 6, 7):
            _fact(session, START + timedelta(minutes=minute), card_bin="555555")

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _kind_keys(result, "bin_onset") == [(START + timedelta(minutes=5), "555555")]


def test_bin_onset_ignores_payments_without_a_bin(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute in (5, 6, 7, 8):
            _fact(session, START + timedelta(minutes=minute), card_bin=None)

    # Act
    result = _timeline(session_factory)

    # Assert
    assert [change for change in result.changes if change.kind == "bin_onset"] == []


# --- change_timeline: incident_started -------------------------------------------------------------------


def test_incident_started_reports_the_merchants_and_platform_wide_incidents_inside_the_window(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    own_at = START + timedelta(minutes=30)
    platform_at = START + timedelta(hours=3)
    with session_factory.begin() as session:
        _incident(session, "inc_own", MERCHANT, own_at)
        _incident(session, "inc_platform", None, platform_at)

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _keys(result) == [
        (own_at, "incident_started", "inc_own"),
        (platform_at, "incident_started", "inc_platform"),
    ]


def test_incident_started_never_reports_another_merchants_incident(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_other", OTHER_MERCHANT, START + timedelta(minutes=30))

    # Act
    result = _timeline(session_factory)

    # Assert
    assert result.changes == ()


def test_incident_started_includes_the_window_start_and_excludes_the_window_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_before", MERCHANT, START - timedelta(seconds=1))
        _incident(session, "inc_at_start", MERCHANT, START)
        _incident(session, "inc_last_second", MERCHANT, END - timedelta(seconds=1))
        _incident(session, "inc_at_end", MERCHANT, END)

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _keys(result) == [
        (START, "incident_started", "inc_at_start"),
        (END - timedelta(seconds=1), "incident_started", "inc_last_second"),
    ]


def test_incident_started_detail_is_a_non_empty_sentence(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_own", MERCHANT, START + timedelta(minutes=30))

    # Act
    result = _timeline(session_factory)

    # Assert
    assert result.changes[0].detail.strip() != ""


# --- change_timeline: ordering ---------------------------------------------------------------------------


def test_changes_are_sorted_by_time_then_kind_then_subject(session_factory: sessionmaker[Session]) -> None:
    # Arrange: a quiet baseline (rate 0) so the onsets are decided by the failure floor alone
    first_failure = START + timedelta(hours=1, minutes=10)
    with session_factory.begin() as session:
        for index in range(20):
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="succeeded",
                card_bin="400000",
            )
        for minute in (10, 11, 12):
            _fact(
                session,
                START + timedelta(hours=1, minutes=minute),
                card_bin="400000",
                decline_code="issuer_unavailable",
            )
        _incident(session, "inc_b", MERCHANT, first_failure)
        _incident(session, "inc_a", None, first_failure)
        _incident(session, "inc_early", MERCHANT, START + timedelta(minutes=30))

    # Act
    result = _timeline(session_factory)

    # Assert
    assert _keys(result) == [
        (START + timedelta(minutes=30), "incident_started", "inc_early"),
        (START + timedelta(hours=1), "failure_onset", "card"),
        (first_failure, "bin_onset", "400000"),
        (first_failure, "incident_started", "inc_a"),
        (first_failure, "incident_started", "inc_b"),
        (first_failure, "new_decline_code", "issuer_unavailable"),
    ]


def test_every_change_event_carries_a_non_empty_detail(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute in (10, 11, 12):
            _fact(session, START + timedelta(minutes=minute), card_bin="400000", decline_code="rate_limited")
        _incident(session, "inc_a", MERCHANT, START + timedelta(minutes=5))

    # Act
    result = _timeline(session_factory)

    # Assert
    assert len(result.changes) == 4
    assert [change.detail.strip() != "" for change in result.changes] == [True, True, True, True]
