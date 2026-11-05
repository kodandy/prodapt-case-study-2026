"""Tests for the pure AML/KYC rules `evaluate_kyc_rules`, written from docs/specs/compliance-rbac.md
section 5 (Rules table)."""

from datetime import datetime, timedelta

import pytest
from gateway.compliance.kyc import (
    AMOUNT_THRESHOLD_MINOR,
    HIGH_RISK_COUNTRIES,
    KYC_MAX_AGE,
    VELOCITY_MAX_TRANSFERS,
    KycFacts,
    evaluate_kyc_rules,
)

from conftest import TEST_NOW

NOW = TEST_NOW
ONE_SECOND = timedelta(seconds=1)
SPEC_THRESHOLDS = {"USD": 1_000_000, "EUR": 1_000_000, "INR": 80_000_000, "JPY": 1_500_000}
CURRENCIES = sorted(SPEC_THRESHOLDS)
UNVERIFIED_STATUSES = ["unverified", "pending", "rejected"]


def _facts(
    *,
    amount_minor: int = 1_000,
    currency: str = "USD",
    volume_24h_minor: int = 0,
    transfers_last_hour: int = 0,
    sender_country: str | None = "US",
    recipient_country: str | None = "US",
    kyc_status: str = "unverified",
    kyc_verified_at: datetime | None = None,
    kyc_expires_at: datetime | None = None,
) -> KycFacts:
    """A quiet baseline: a small domestic transfer by an unverified sender, so no rule fires."""
    return KycFacts(
        amount_minor=amount_minor,
        currency=currency,
        volume_24h_minor=volume_24h_minor,
        transfers_last_hour=transfers_last_hour,
        sender_country=sender_country,
        recipient_country=recipient_country,
        kyc_status=kyc_status,
        kyc_verified_at=kyc_verified_at,
        kyc_expires_at=kyc_expires_at,
    )


def _verified(expires_in: timedelta, verified_ago: timedelta) -> KycFacts:
    return _facts(
        kyc_status="verified",
        kyc_verified_at=NOW - verified_ago,
        kyc_expires_at=NOW + expires_in,
    )


# --- constants -------------------------------------------------------------------------------------------


def test_amount_thresholds_are_the_specified_minor_units_per_currency() -> None:
    # Arrange / Act
    thresholds = dict(AMOUNT_THRESHOLD_MINOR)

    # Assert
    assert thresholds == SPEC_THRESHOLDS


def test_high_risk_countries_are_kp_ir_and_mm() -> None:
    # Arrange / Act
    countries = set(HIGH_RISK_COUNTRIES)

    # Assert
    assert countries == {"KP", "IR", "MM"}


def test_kyc_max_age_is_365_days_and_velocity_limit_is_10() -> None:
    # Arrange / Act
    limits = (KYC_MAX_AGE, VELOCITY_MAX_TRANSFERS)

    # Assert
    assert limits == (timedelta(days=365), 10)


# --- baseline --------------------------------------------------------------------------------------------


def test_a_small_domestic_transfer_fires_no_rule() -> None:
    # Arrange
    facts = _facts()

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


def test_a_verified_sender_with_fresh_kyc_fires_no_rule() -> None:
    # Arrange
    facts = _verified(expires_in=timedelta(days=200), verified_ago=timedelta(days=165))

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


# --- amount_threshold ------------------------------------------------------------------------------------


@pytest.mark.parametrize("currency", CURRENCIES)
def test_amount_threshold_fires_exactly_at_the_currency_threshold(currency: str) -> None:
    # Arrange
    facts = _facts(currency=currency, amount_minor=SPEC_THRESHOLDS[currency])

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("amount_threshold",)


@pytest.mark.parametrize("currency", CURRENCIES)
def test_amount_threshold_does_not_fire_one_minor_unit_below_the_threshold(currency: str) -> None:
    # Arrange
    facts = _facts(currency=currency, amount_minor=SPEC_THRESHOLDS[currency] - 1)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


def test_amount_threshold_is_per_currency_so_a_usd_threshold_amount_is_small_in_inr() -> None:
    # Arrange
    facts = _facts(currency="INR", amount_minor=SPEC_THRESHOLDS["USD"])

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


# --- volume_24h ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("currency", CURRENCIES)
def test_volume_24h_fires_when_prior_volume_plus_amount_reaches_twice_the_threshold(currency: str) -> None:
    # Arrange
    threshold = SPEC_THRESHOLDS[currency]
    amount = threshold - 1
    facts = _facts(currency=currency, amount_minor=amount, volume_24h_minor=2 * threshold - amount)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("volume_24h",)


@pytest.mark.parametrize("currency", CURRENCIES)
def test_volume_24h_does_not_fire_one_minor_unit_below_twice_the_threshold(currency: str) -> None:
    # Arrange
    threshold = SPEC_THRESHOLDS[currency]
    amount = threshold - 1
    facts = _facts(currency=currency, amount_minor=amount, volume_24h_minor=2 * threshold - amount - 1)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


def test_volume_24h_counts_the_prior_volume_only_so_zero_prior_volume_needs_twice_the_threshold() -> None:
    # Arrange
    facts = _facts(amount_minor=2 * SPEC_THRESHOLDS["USD"] - 1, volume_24h_minor=0)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("amount_threshold",)


# --- high_risk_country_pair ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender", "recipient"),
    [("US", "IR"), ("IR", "US"), ("US", "KP"), ("MM", "DE"), ("KP", "IR"), ("DE", "MM")],
)
def test_high_risk_country_pair_fires_when_one_side_is_high_risk_and_the_countries_differ(
    sender: str, recipient: str
) -> None:
    # Arrange
    facts = _facts(sender_country=sender, recipient_country=recipient)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("high_risk_country_pair",)


@pytest.mark.parametrize(("sender", "recipient"), [("IR", "IR"), ("KP", "KP"), ("MM", "MM")])
def test_high_risk_country_pair_does_not_fire_for_a_domestic_high_risk_transfer(
    sender: str, recipient: str
) -> None:
    # Arrange
    facts = _facts(sender_country=sender, recipient_country=recipient)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


@pytest.mark.parametrize(("sender", "recipient"), [("US", "US"), ("US", "DE"), ("DE", "FR")])
def test_high_risk_country_pair_does_not_fire_when_neither_country_is_high_risk(
    sender: str, recipient: str
) -> None:
    # Arrange
    facts = _facts(sender_country=sender, recipient_country=recipient)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


# --- kyc_expired -----------------------------------------------------------------------------------------


def test_kyc_expired_fires_when_the_expiry_equals_now() -> None:
    # Arrange
    facts = _verified(expires_in=timedelta(0), verified_ago=timedelta(days=300))

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("kyc_expired",)


def test_kyc_expired_does_not_fire_one_second_before_the_expiry() -> None:
    # Arrange
    facts = _verified(expires_in=ONE_SECOND, verified_ago=timedelta(days=300))

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


def test_kyc_expired_fires_when_the_expiry_is_in_the_past() -> None:
    # Arrange
    facts = _verified(expires_in=-ONE_SECOND, verified_ago=timedelta(days=300))

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("kyc_expired",)


@pytest.mark.parametrize("status", UNVERIFIED_STATUSES)
def test_kyc_expired_and_kyc_stale_fire_only_for_a_verified_status(status: str) -> None:
    # Arrange
    facts = _facts(
        kyc_status=status,
        kyc_verified_at=NOW - KYC_MAX_AGE - timedelta(days=1),
        kyc_expires_at=NOW - timedelta(days=1),
    )

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


# --- kyc_stale -------------------------------------------------------------------------------------------


def test_kyc_stale_fires_when_the_verification_is_exactly_365_days_old() -> None:
    # Arrange
    facts = _verified(expires_in=timedelta(days=30), verified_ago=timedelta(days=365))

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("kyc_stale",)


def test_kyc_stale_does_not_fire_one_second_before_365_days() -> None:
    # Arrange
    facts = _verified(expires_in=timedelta(days=30), verified_ago=timedelta(days=365) - ONE_SECOND)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


def test_kyc_stale_fires_when_the_verification_is_older_than_365_days() -> None:
    # Arrange
    facts = _verified(expires_in=timedelta(days=30), verified_ago=timedelta(days=365) + ONE_SECOND)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("kyc_stale",)


# --- velocity --------------------------------------------------------------------------------------------


def test_velocity_fires_at_ten_prior_transfers_in_the_last_hour() -> None:
    # Arrange
    facts = _facts(transfers_last_hour=10)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("velocity",)


def test_velocity_does_not_fire_at_nine_prior_transfers() -> None:
    # Arrange
    facts = _facts(transfers_last_hour=9)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ()


def test_velocity_fires_above_the_limit() -> None:
    # Arrange
    facts = _facts(transfers_last_hour=11)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("velocity",)


# --- combinations and order ------------------------------------------------------------------------------


def test_amount_and_volume_rules_fire_together_in_table_order() -> None:
    # Arrange
    threshold = SPEC_THRESHOLDS["USD"]
    facts = _facts(amount_minor=threshold, volume_24h_minor=threshold)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("amount_threshold", "volume_24h")


def test_all_six_facts_based_rules_fire_in_the_order_of_the_spec_table() -> None:
    # Arrange
    threshold = SPEC_THRESHOLDS["USD"]
    facts = _facts(
        amount_minor=threshold,
        volume_24h_minor=threshold,
        transfers_last_hour=10,
        sender_country="US",
        recipient_country="IR",
        kyc_status="verified",
        kyc_verified_at=NOW - KYC_MAX_AGE,
        kyc_expires_at=NOW,
    )

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == (
        "amount_threshold",
        "volume_24h",
        "high_risk_country_pair",
        "kyc_expired",
        "kyc_stale",
        "velocity",
    )


def test_amount_and_velocity_fire_together_skipping_the_rules_between_them() -> None:
    # Arrange
    facts = _facts(amount_minor=SPEC_THRESHOLDS["USD"], transfers_last_hour=10)

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("amount_threshold", "velocity")


def test_expired_and_stale_fire_together_in_table_order() -> None:
    # Arrange
    facts = _verified(expires_in=-ONE_SECOND, verified_ago=KYC_MAX_AGE + timedelta(days=1))

    # Act
    rules = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert rules == ("kyc_expired", "kyc_stale")


def test_evaluation_is_pure_and_repeatable_for_the_same_facts() -> None:
    # Arrange
    facts = _facts(amount_minor=SPEC_THRESHOLDS["USD"], transfers_last_hour=10)

    # Act
    first = evaluate_kyc_rules(facts, NOW)
    second = evaluate_kyc_rules(facts, NOW)

    # Assert
    assert first == second == ("amount_threshold", "velocity")
