import re

import pytest
from gateway.payments.tokenization.cards import TEST_PANS, card_fingerprint, luhn_valid

SORTED_PANS = sorted(TEST_PANS)
PAN = "4242424242424242"
KEY = "fingerprint-key-one"


def _change_last_digit(pan: str) -> str:
    return pan[:-1] + str((int(pan[-1]) + 1) % 10)


@pytest.mark.parametrize("pan", SORTED_PANS)
def test_luhn_valid_accepts_every_test_pan(pan: str) -> None:
    # Arrange: pan comes from the allowlist

    # Act
    result = luhn_valid(pan)

    # Assert
    assert result is True


@pytest.mark.parametrize("pan", SORTED_PANS)
def test_luhn_valid_rejects_a_one_digit_change(pan: str) -> None:
    # Arrange
    mutated = _change_last_digit(pan)

    # Act
    result = luhn_valid(mutated)

    # Assert
    assert result is False


@pytest.mark.parametrize("pan", ["", "abcd"])
def test_luhn_valid_rejects_non_digit_input(pan: str) -> None:
    # Arrange: input that is not a plain digit string

    # Act
    result = luhn_valid(pan)

    # Assert
    assert result is False


def test_card_fingerprint_is_deterministic_for_the_same_key() -> None:
    # Arrange / Act
    first = card_fingerprint(PAN, KEY)
    second = card_fingerprint(PAN, KEY)

    # Assert
    assert first == second


def test_card_fingerprint_differs_for_a_different_key() -> None:
    # Arrange / Act
    with_key_one = card_fingerprint(PAN, KEY)
    with_key_two = card_fingerprint(PAN, "fingerprint-key-two")

    # Assert
    assert with_key_one != with_key_two


def test_card_fingerprint_differs_for_a_different_pan() -> None:
    # Arrange / Act
    first = card_fingerprint("4242424242424242", KEY)
    second = card_fingerprint("5555555555554444", KEY)

    # Assert
    assert first != second


@pytest.mark.parametrize("pan", SORTED_PANS)
def test_card_fingerprint_is_64_lowercase_hex_chars_without_the_pan(pan: str) -> None:
    # Arrange / Act
    fingerprint = card_fingerprint(pan, KEY)

    # Assert
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint) is not None
    assert pan not in fingerprint
