"""Pure rules of the short-term conversation state, written from docs/specs/memory.md section B10.2."""

from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.memory.state import (
    EMPTY_STATE,
    MAX_MEMORY_CHARS,
    ConversationState,
    TimeWindow,
    detect_memory_request,
    detect_metric,
    detect_window,
    resolve,
)

from conftest import TEST_NOW

MIDNIGHT_TODAY = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
MIDNIGHT_YESTERDAY = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
YESTERDAY = TimeWindow(start=MIDNIGHT_YESTERDAY, end=MIDNIGHT_TODAY)
TODAY = TimeWindow(start=MIDNIGHT_TODAY, end=TEST_NOW)
DEFAULT_WINDOW = TimeWindow(start=TEST_NOW - timedelta(hours=24), end=TEST_NOW)


# --- detect_metric ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("what is my card failure rate?", "failure_rate"),
        ("How many payments were DECLINED?", "failure_rate"),
        ("any errors this morning", "failure_rate"),
        ("what is my payment volume?", "payment_volume"),
        ("How many payments did I get?", "payment_volume"),
        ("number of payments please", "payment_volume"),
        ("show the payment count", "payment_volume"),
        ("VOLUME", "payment_volume"),
        ("failure volume", "failure_rate"),
        ("what is my settlement balance?", None),
        ("", None),
    ],
    ids=[
        "fail",
        "declin_case_insensitive_wins_over_how_many_payments",
        "error",
        "volume",
        "how_many_payments",
        "number_of_payments",
        "payment_count",
        "volume_upper_case",
        "failure_wins_over_volume",
        "no_metric_word",
        "empty",
    ],
)
def test_detect_metric_maps_keywords_to_the_metric(question: str, expected: str | None) -> None:
    # Arrange: the parameters are the arrangement

    # Act
    metric = detect_metric(question)

    # Assert
    assert metric == expected


# --- detect_window ---------------------------------------------------------------------------------------


def test_detect_window_yesterday_is_the_previous_utc_day() -> None:
    # Arrange
    question = "what about yesterday?"

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window == YESTERDAY


def test_detect_window_today_runs_from_utc_midnight_to_now() -> None:
    # Arrange
    question = "my failure rate today"

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window == TODAY


def test_detect_window_today_shortly_after_midnight_is_a_short_window() -> None:
    # Arrange
    now = MIDNIGHT_TODAY + timedelta(minutes=30)

    # Act
    window = detect_window("failures today", now)

    # Assert
    assert window == TimeWindow(start=MIDNIGHT_TODAY, end=now)


def test_detect_window_yesterday_is_the_full_previous_day_even_just_after_midnight() -> None:
    # Arrange
    now = MIDNIGHT_TODAY + timedelta(minutes=30)

    # Act
    window = detect_window("failures yesterday", now)

    # Assert
    assert window == YESTERDAY


def test_detect_window_yesterday_wins_over_today_in_the_same_question() -> None:
    # Arrange
    question = "compare today with yesterday"

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window == YESTERDAY


def test_detect_window_is_case_insensitive() -> None:
    # Arrange
    question = "YESTERDAY please"

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window == YESTERDAY


@pytest.mark.parametrize(
    ("question", "delta"),
    [
        ("failures in the last 6 hours", timedelta(hours=6)),
        ("failures in the past 1 hour", timedelta(hours=1)),
        ("failures in the last 3 days", timedelta(days=3)),
        ("failures in the past 1 day", timedelta(days=1)),
        ("failures in the last 720 hours", timedelta(hours=720)),
        ("failures in the last 90 days", timedelta(days=90)),
        ("failures in the last week", timedelta(days=7)),
    ],
    ids=["last_6_hours", "past_1_hour", "last_3_days", "past_1_day", "max_hours", "max_days", "last_week"],
)
def test_detect_window_relative_phrases_end_at_now(question: str, delta: timedelta) -> None:
    # Arrange: the parameters are the arrangement

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window == TimeWindow(start=TEST_NOW - delta, end=TEST_NOW)


@pytest.mark.parametrize(
    "question",
    [
        "failures in the last 721 hours",
        "failures in the last 0 hours",
        "failures in the last 91 days",
        "failures in the last 0 days",
        "failures in the last many days",
    ],
    ids=["hours_over_max", "zero_hours", "days_over_max", "zero_days", "no_number"],
)
def test_detect_window_rejects_out_of_range_counts(question: str) -> None:
    # Arrange: the parameters are the arrangement

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window is None


@pytest.mark.parametrize(
    "question", ["what is my failure rate?", "", "in the last"], ids=["none", "empty", "stub"]
)
def test_detect_window_without_a_time_phrase_returns_none(question: str) -> None:
    # Arrange: the parameters are the arrangement

    # Act
    window = detect_window(question, TEST_NOW)

    # Assert
    assert window is None


# --- detect_memory_request -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("remember that refunds go through acq_b", "refunds go through acq_b"),
        ("remember: refunds go through acq_b", "refunds go through acq_b"),
        ("REMEMBER THAT Refunds Go Through acq_b", "Refunds Go Through acq_b"),
        ("Remember: mixed case prefix", "mixed case prefix"),
        ("   remember that   padded content   ", "padded content"),
    ],
    ids=["that_prefix", "colon_prefix", "upper_case_prefix", "capitalised", "padding_stripped"],
)
def test_detect_memory_request_returns_the_content_after_the_prefix(question: str, expected: str) -> None:
    # Arrange: the parameters are the arrangement

    # Act
    content = detect_memory_request(question)

    # Assert
    assert content == expected


@pytest.mark.parametrize(
    "question",
    [
        "",
        "remember that",
        "remember that    ",
        "remember:",
        "remember thatrefunds",
        "please remember that refunds go through acq_b",
        "what is my failure rate? remember that x",
        "what is my failure rate?",
    ],
    ids=[
        "empty",
        "prefix_only",
        "prefix_and_blanks",
        "colon_only",
        "no_space_after_prefix",
        "prefix_not_at_start",
        "prefix_in_the_middle",
        "ordinary_question",
    ],
)
def test_detect_memory_request_returns_none_when_it_is_not_a_memory_request(question: str) -> None:
    # Arrange: the parameters are the arrangement

    # Act
    content = detect_memory_request(question)

    # Assert
    assert content is None


def test_detect_memory_request_accepts_content_of_exactly_the_maximum_length() -> None:
    # Arrange
    content = "x" * MAX_MEMORY_CHARS

    # Act
    detected = detect_memory_request(f"remember that {content}")

    # Assert
    assert MAX_MEMORY_CHARS == 500
    assert detected == content


def test_detect_memory_request_rejects_content_one_over_the_maximum_length() -> None:
    # Arrange
    content = "x" * (MAX_MEMORY_CHARS + 1)

    # Act
    detected = detect_memory_request(f"remember that {content}")

    # Assert
    assert detected is None


# --- resolve ---------------------------------------------------------------------------------------------


def test_resolve_follow_up_keeps_the_metric_and_moves_the_window_to_the_previous_utc_day() -> None:
    # Arrange
    previous = ConversationState(metric="failure_rate", window=TODAY)

    # Act
    intent, state = resolve("what about yesterday?", previous, TEST_NOW)

    # Assert
    assert intent == "metric_query"
    assert state == ConversationState(metric="failure_rate", window=YESTERDAY)


def test_resolve_first_question_sets_metric_and_window_from_the_text() -> None:
    # Arrange
    question = "what is my card failure rate today?"

    # Act
    intent, state = resolve(question, EMPTY_STATE, TEST_NOW)

    # Assert
    assert intent == "metric_query"
    assert state == ConversationState(metric="failure_rate", window=TODAY)


def test_resolve_without_any_time_phrase_defaults_to_the_last_24_hours() -> None:
    # Arrange
    question = "what is my failure rate?"

    # Act
    intent, state = resolve(question, EMPTY_STATE, TEST_NOW)

    # Assert
    assert intent == "metric_query"
    assert state == ConversationState(metric="failure_rate", window=DEFAULT_WINDOW)


def test_resolve_keeps_the_previous_window_when_only_the_metric_changes() -> None:
    # Arrange
    previous = ConversationState(metric="payment_volume", window=YESTERDAY)

    # Act
    intent, state = resolve("and the failure rate?", previous, TEST_NOW)

    # Assert
    assert intent == "metric_query"
    assert state == ConversationState(metric="failure_rate", window=YESTERDAY)


def test_resolve_a_new_metric_replaces_the_previous_one() -> None:
    # Arrange
    previous = ConversationState(metric="failure_rate", window=TODAY)

    # Act
    intent, state = resolve("what is my payment volume?", previous, TEST_NOW)

    # Assert
    assert intent == "metric_query"
    assert state == ConversationState(metric="payment_volume", window=TODAY)


def test_resolve_a_new_window_without_a_metric_keeps_the_previous_metric() -> None:
    # Arrange
    previous = ConversationState(metric="payment_volume", window=TODAY)

    # Act
    intent, state = resolve("and over the last 3 days?", previous, TEST_NOW)

    # Assert
    assert intent == "metric_query"
    assert state == ConversationState(
        metric="payment_volume", window=TimeWindow(start=TEST_NOW - timedelta(days=3), end=TEST_NOW)
    )


def test_resolve_without_a_metric_anywhere_is_unsupported_and_keeps_the_state() -> None:
    # Arrange
    question = "what is my settlement balance?"

    # Act
    intent, state = resolve(question, EMPTY_STATE, TEST_NOW)

    # Assert
    assert intent == "unsupported"
    assert state == EMPTY_STATE


def test_resolve_unsupported_keeps_a_previous_window_that_has_no_metric() -> None:
    # Arrange
    previous = ConversationState(metric=None, window=YESTERDAY)

    # Act
    intent, state = resolve("what about the balance?", previous, TEST_NOW)

    # Assert
    assert intent == "unsupported"
    assert state == previous


def test_resolve_memory_proposal_keeps_the_previous_state_unchanged() -> None:
    # Arrange
    previous = ConversationState(metric="failure_rate", window=TODAY)

    # Act
    intent, state = resolve("remember that refunds go through acq_b", previous, TEST_NOW)

    # Assert
    assert intent == "memory_proposal"
    assert state == previous


def test_resolve_memory_prefix_wins_over_metric_and_window_words() -> None:
    # Arrange
    question = "remember that failures spike yesterday evening"

    # Act
    intent, state = resolve(question, EMPTY_STATE, TEST_NOW)

    # Assert
    assert intent == "memory_proposal"
    assert state == EMPTY_STATE


def test_resolve_an_over_long_memory_request_is_not_a_proposal() -> None:
    # Arrange
    question = "remember that " + "x" * (MAX_MEMORY_CHARS + 1)

    # Act
    intent, state = resolve(question, EMPTY_STATE, TEST_NOW)

    # Assert
    assert intent == "unsupported"
    assert state == EMPTY_STATE
