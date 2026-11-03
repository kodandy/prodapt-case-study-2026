"""Tests for ops_ai/investigation/taxonomy.py, written from docs/specs/sq1.md section 3."""

import pytest
from ops_ai.investigation.taxonomy import failure_code


@pytest.mark.parametrize(
    ("failure_reason", "code"),
    [
        ("Issuer unavailable (timeout) for BIN 400000", "issuer_unavailable"),
        ("Issuer unavailable (timeout)", "issuer_unavailable"),
        ("issuer unavailable", "issuer_unavailable"),
        ("Gateway timeout", "issuer_unavailable"),
        ("Issuer declined: do_not_honor", "do_not_honor"),
        ("Insufficient funds on card", "insufficient_funds"),
        ("Card expired", "expired_card"),
        ("Incorrect CVC", "incorrect_cvc"),
        ("Flagged by fraud model: velocity anomaly", "fraud_flagged"),
        ("Wallet balance too low", "other"),
        ("UPI PIN incorrect", "other"),
        ("", "other"),
    ],
)
def test_failure_code_maps_a_reason_to_its_code(failure_reason: str, code: str) -> None:
    # Arrange: reasons are the dataset's CARD_FAILURES / FLAG_REASONS plus near misses

    # Act
    result = failure_code(failure_reason)

    # Assert
    assert result == code


@pytest.mark.parametrize(
    ("failure_reason", "code"),
    [
        ("ISSUER UNAVAILABLE", "issuer_unavailable"),
        ("TIMEOUT", "issuer_unavailable"),
        ("DO_NOT_HONOR", "do_not_honor"),
        ("INSUFFICIENT FUNDS", "insufficient_funds"),
        ("CARD EXPIRED", "expired_card"),
        ("incorrect cvc", "incorrect_cvc"),
        ("FRAUD model hit", "fraud_flagged"),
    ],
)
def test_failure_code_is_case_insensitive(failure_reason: str, code: str) -> None:
    # Arrange: the same reasons in another case

    # Act
    result = failure_code(failure_reason)

    # Assert
    assert result == code


@pytest.mark.parametrize(
    ("failure_reason", "code"),
    [
        ("Card expired after a timeout", "issuer_unavailable"),
        ("Issuer unavailable, do_not_honor", "issuer_unavailable"),
        ("do_not_honor with insufficient funds", "do_not_honor"),
        ("Insufficient funds, card expired", "insufficient_funds"),
        ("Card expired, wrong cvc", "expired_card"),
        ("Incorrect CVC flagged as fraud", "incorrect_cvc"),
    ],
)
def test_failure_code_returns_the_first_matching_rule_in_table_order(failure_reason: str, code: str) -> None:
    # Arrange: reasons that match several rules; table order decides

    # Act
    result = failure_code(failure_reason)

    # Assert
    assert result == code
