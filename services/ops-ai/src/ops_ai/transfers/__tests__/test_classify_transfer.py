"""Tests for classify_transfer, written from docs/specs/sq2.md section 5 (first match wins)."""

import uuid
from datetime import UTC, datetime

import pytest
from ops_ai.transfers import TransferStatus, classify_transfer

TRANSFER_ID = uuid.UUID("5f1b7c2e-8d4a-4e6b-9a3c-0d2e4f6a8b10")
DECIDED_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
XB = "cross_border_limit_kyc_tier"
LIMIT_CASE = ("cross_border_limit_for_kyc_tier", "sender")
UNCLASSIFIED = ("unclassified", "unknown")


def _status(
    *,
    status: str = "failed",
    reason_code: str | None = XB,
    amount_minor: int = 180_000,
    currency: str = "USD",
    country_sender: str = "US",
    country_receiver: str = "IN",
    kyc_tier: int | None = 1,
    limit_minor: int | None = 100_000,
    limit_currency: str | None = "USD",
    next_tier_limit_minor: int | None = 1_000_000,
    sender_balance_minor: int | None = 525_000,
) -> TransferStatus:
    return TransferStatus(
        transfer_id=TRANSFER_ID,
        amount_minor=amount_minor,
        currency=currency,
        method="bank_transfer",
        status=status,
        country_sender=country_sender,
        country_receiver=country_receiver,
        reason_code=reason_code,
        kyc_tier=kyc_tier,
        limit_minor=limit_minor,
        limit_currency=limit_currency,
        next_tier_limit_minor=next_tier_limit_minor,
        sender_balance_minor=sender_balance_minor,
        decided_at=DECIDED_AT,
    )


# --- not declined (first rule) ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["success", "pending", "flagged", "reversed"])
def test_a_transfer_that_did_not_fail_is_not_declined_even_when_the_limit_numbers_would_match(
    status: str,
) -> None:
    # Arrange
    transfer = _status(status=status)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("not_declined", "unknown")


def test_a_succeeded_transfer_with_no_reason_code_is_not_declined() -> None:
    # Arrange
    transfer = _status(
        status="success",
        reason_code=None,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
    )

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("not_declined", "unknown")


# --- cross-border limit for the KYC tier ------------------------------------------------------------------


def test_the_planted_shape_is_a_cross_border_limit_the_sender_can_fix() -> None:
    # Arrange
    transfer = _status()

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == LIMIT_CASE


def test_an_amount_one_minor_unit_above_the_limit_is_classified_when_the_balance_covers_it() -> None:
    # Arrange
    transfer = _status(amount_minor=100_001, sender_balance_minor=100_001)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == LIMIT_CASE


def test_an_amount_equal_to_the_limit_is_unclassified_because_the_limit_was_not_exceeded() -> None:
    # Arrange
    transfer = _status(amount_minor=100_000, sender_balance_minor=525_000)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_an_amount_below_the_limit_contradicts_the_reason_code_and_is_unclassified() -> None:
    # Arrange
    transfer = _status(amount_minor=50_000)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_a_balance_equal_to_the_amount_still_counts_as_covering_it() -> None:
    # Arrange
    transfer = _status(amount_minor=180_000, sender_balance_minor=180_000)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == LIMIT_CASE


def test_a_balance_one_minor_unit_short_of_the_amount_is_unclassified_not_a_funds_failure() -> None:
    # Arrange
    transfer = _status(amount_minor=180_000, sender_balance_minor=179_999)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_a_missing_sender_balance_is_unclassified() -> None:
    # Arrange
    transfer = _status(sender_balance_minor=None)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_a_missing_limit_is_unclassified() -> None:
    # Arrange
    transfer = _status(limit_minor=None)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_a_limit_in_another_currency_than_the_transfer_is_unclassified() -> None:
    # Arrange
    transfer = _status(currency="EUR", limit_currency="USD")

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_a_missing_limit_currency_is_unclassified() -> None:
    # Arrange
    transfer = _status(limit_currency=None)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_a_domestic_transfer_with_the_cross_border_reason_code_is_unclassified() -> None:
    # Arrange
    transfer = _status(country_sender="US", country_receiver="US")

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


# --- insufficient funds ----------------------------------------------------------------------------------


def test_insufficient_funds_with_a_balance_below_the_amount_is_the_sender_to_fix() -> None:
    # Arrange
    transfer = _status(
        reason_code="insufficient_funds",
        amount_minor=180_000,
        sender_balance_minor=40_000,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
    )

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("insufficient_funds", "sender")


def test_insufficient_funds_with_a_balance_equal_to_the_amount_contradicts_the_record() -> None:
    # Arrange
    transfer = _status(reason_code="insufficient_funds", amount_minor=180_000, sender_balance_minor=180_000)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_insufficient_funds_with_a_balance_above_the_amount_contradicts_the_record() -> None:
    # Arrange
    transfer = _status(reason_code="insufficient_funds", amount_minor=180_000, sender_balance_minor=525_000)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_insufficient_funds_without_a_balance_is_unclassified() -> None:
    # Arrange
    transfer = _status(reason_code="insufficient_funds", sender_balance_minor=None)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


def test_the_cross_border_reason_code_with_a_short_balance_does_not_fall_through_to_insufficient_funds() -> (
    None
):
    # Arrange
    transfer = _status(reason_code=XB, amount_minor=180_000, sender_balance_minor=40_000)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED


# --- KYC verification ------------------------------------------------------------------------------------


@pytest.mark.parametrize("sender_balance_minor", [525_000, 40_000, None], ids=["rich", "short", "unknown"])
def test_kyc_verification_failed_is_the_sender_to_fix_whatever_the_numbers(
    sender_balance_minor: int | None,
) -> None:
    # Arrange
    transfer = _status(
        reason_code="kyc_verification_failed",
        sender_balance_minor=sender_balance_minor,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
    )

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("kyc_verification_failed", "sender")


# --- everything else is unclassified, owner unknown ------------------------------------------------------


@pytest.mark.parametrize(
    "reason_code",
    ["beneficiary_rejected", "provider_error", "some_new_reason_code", "", None],
    ids=["beneficiary_rejected", "provider_error", "unknown_code", "empty_code", "no_code"],
)
def test_a_failed_transfer_with_any_other_reason_code_is_unclassified_and_the_owner_unknown(
    reason_code: str | None,
) -> None:
    # Arrange
    transfer = _status(reason_code=reason_code)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == UNCLASSIFIED
