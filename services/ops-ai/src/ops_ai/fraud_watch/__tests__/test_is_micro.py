"""Tests for the card-testing constants and is_micro, written from docs/specs/sq3.md section 4."""

import pytest
from ops_ai.fraud_watch import (
    CURRENCY_EXPONENT,
    EPISODE_GAP_SECONDS,
    LIVE_LOOKBACK_MINUTES,
    MICRO_MAX_UNITS,
    MIN_DISTINCT_CARDS,
    MIN_MICRO_PAYMENTS,
    MIN_MICRO_SHARE,
    is_micro,
)


def test_the_thresholds_are_the_specified_constants() -> None:
    # Arrange / Act
    observed = (
        MICRO_MAX_UNITS,
        CURRENCY_EXPONENT,
        EPISODE_GAP_SECONDS,
        MIN_MICRO_PAYMENTS,
        MIN_DISTINCT_CARDS,
        MIN_MICRO_SHARE,
        LIVE_LOOKBACK_MINUTES,
    )

    # Assert
    assert observed == (1, {"JPY": 0}, 120, 10, 3, 0.8, 30)


@pytest.mark.parametrize(
    ("amount_minor", "currency", "expected"),
    [
        (100, "USD", True),
        (101, "USD", False),
        (1, "USD", True),
        (0, "USD", True),
        (100, "EUR", True),
        (101, "EUR", False),
        (100, "INR", True),
        (101, "INR", False),
        (1, "JPY", True),
        (2, "JPY", False),
        (0, "JPY", True),
        (100, "JPY", False),
        (100, "XYZ", True),
        (101, "XYZ", False),
    ],
    ids=[
        "usd_exactly_1_00",
        "usd_1_01",
        "usd_0_01",
        "usd_zero",
        "eur_exactly_1_00",
        "eur_1_01",
        "inr_exactly_1_00",
        "inr_1_01",
        "jpy_exactly_1",
        "jpy_2",
        "jpy_zero",
        "jpy_100_is_a_hundred_yen",
        "unknown_currency_uses_exponent_2_at_the_limit",
        "unknown_currency_uses_exponent_2_above_the_limit",
    ],
)
def test_is_micro_compares_the_amount_with_one_unit_of_the_currency(
    amount_minor: int, currency: str, expected: bool
) -> None:
    # Arrange: amount_minor <= MICRO_MAX_UNITS * 10 ** exponent, exponent 0 for JPY and 2 otherwise

    # Act
    result = is_micro(amount_minor, currency)

    # Assert
    assert result is expected
