"""Tests for derive_card_ref, written from docs/specs/sq3.md section 2 and the H-4 hardening section."""

import hashlib
import hmac
import re
import uuid

import pytest
from gateway.payments.tokenization.cards import TEST_PANS, card_fingerprint, derive_card_ref

SORTED_PANS = sorted(TEST_PANS)
KEY = "fingerprint-key-one"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
FINGERPRINT = card_fingerprint("4242424242424242", KEY)
OTHER_FINGERPRINT = card_fingerprint("5555555555554444", KEY)


def _expected(fingerprint: str, merchant_id: uuid.UUID, key: str) -> str:
    subkey = hmac.new(key.encode(), b"card_ref-key:v1", hashlib.sha256).digest()
    return hmac.new(subkey, f"{merchant_id}:{fingerprint}".encode(), hashlib.sha256).hexdigest()[:32]


def test_derive_card_ref_is_the_merchant_bound_hmac_under_its_own_subkey_truncated_to_32_hex() -> None:
    # Arrange
    subkey = hmac.new(KEY.encode(), b"card_ref-key:v1", hashlib.sha256).digest()
    message = f"{MERCHANT}:{FINGERPRINT}".encode()
    expected = hmac.new(subkey, message, hashlib.sha256).hexdigest()[:32]

    # Act
    card_ref = derive_card_ref(FINGERPRINT, MERCHANT, KEY)

    # Assert
    assert card_ref == expected


def test_derive_card_ref_renders_the_merchant_id_as_a_lowercase_hyphenated_uuid() -> None:
    # Arrange
    merchant = uuid.UUID("ABCDEF01-2345-4678-89AB-CDEF01234567")
    subkey = hmac.new(KEY.encode(), b"card_ref-key:v1", hashlib.sha256).digest()
    message = f"abcdef01-2345-4678-89ab-cdef01234567:{FINGERPRINT}".encode()
    expected = hmac.new(subkey, message, hashlib.sha256).hexdigest()[:32]

    # Act
    card_ref = derive_card_ref(FINGERPRINT, merchant, KEY)

    # Assert
    assert card_ref == expected


def test_derive_card_ref_is_deterministic_for_the_same_fingerprint_merchant_and_key() -> None:
    # Arrange / Act
    first = derive_card_ref(FINGERPRINT, MERCHANT, KEY)
    second = derive_card_ref(FINGERPRINT, MERCHANT, KEY)

    # Assert
    assert first == second


def test_derive_card_ref_gives_the_same_pan_at_the_same_merchant_the_same_value() -> None:
    # Arrange
    fingerprint_again = card_fingerprint("4242424242424242", KEY)

    # Act
    first = derive_card_ref(FINGERPRINT, MERCHANT, KEY)
    second = derive_card_ref(fingerprint_again, MERCHANT, KEY)

    # Assert
    assert first == second


def test_derive_card_ref_gives_the_same_pan_at_two_merchants_different_values() -> None:
    # Arrange / Act
    at_first = derive_card_ref(FINGERPRINT, MERCHANT, KEY)
    at_second = derive_card_ref(FINGERPRINT, OTHER_MERCHANT, KEY)

    # Assert
    assert at_first == _expected(FINGERPRINT, MERCHANT, KEY)
    assert at_second == _expected(FINGERPRINT, OTHER_MERCHANT, KEY)
    assert at_first != at_second


def test_derive_card_ref_differs_for_different_fingerprints_at_one_merchant() -> None:
    # Arrange / Act
    first = derive_card_ref(FINGERPRINT, MERCHANT, KEY)
    second = derive_card_ref(OTHER_FINGERPRINT, MERCHANT, KEY)

    # Assert
    assert first != second


def test_derive_card_ref_differs_for_a_different_key() -> None:
    # Arrange / Act
    with_key_one = derive_card_ref(FINGERPRINT, MERCHANT, KEY)
    with_key_two = derive_card_ref(FINGERPRINT, MERCHANT, "fingerprint-key-two")

    # Assert
    assert with_key_one == _expected(FINGERPRINT, MERCHANT, KEY)
    assert with_key_two == _expected(FINGERPRINT, MERCHANT, "fingerprint-key-two")
    assert with_key_one != with_key_two


@pytest.mark.parametrize("pan", SORTED_PANS)
def test_derive_card_ref_is_32_lowercase_hex_chars_and_not_a_prefix_of_the_fingerprint(pan: str) -> None:
    # Arrange
    fingerprint = card_fingerprint(pan, KEY)

    # Act
    card_ref = derive_card_ref(fingerprint, MERCHANT, KEY)

    # Assert
    assert re.fullmatch(r"[0-9a-f]{32}", card_ref) is not None
    assert not fingerprint.startswith(card_ref)
    assert card_ref != fingerprint[:32]


def test_derive_card_ref_does_not_use_the_raw_key_so_it_is_not_the_plain_merchant_bound_hmac() -> None:
    # Arrange
    raw_key_hmac = hmac.new(KEY.encode(), f"{MERCHANT}:{FINGERPRINT}".encode(), hashlib.sha256)
    tagged_raw_key_hmac = hmac.new(
        KEY.encode(), f"card_ref:v1:{MERCHANT}:{FINGERPRINT}".encode(), hashlib.sha256
    )

    # Act
    card_ref = derive_card_ref(FINGERPRINT, MERCHANT, KEY)

    # Assert
    assert card_ref == _expected(FINGERPRINT, MERCHANT, KEY)
    assert card_ref != raw_key_hmac.hexdigest()[:32]
    assert card_ref != tagged_raw_key_hmac.hexdigest()[:32]


def test_derive_card_ref_is_not_the_first_cut_formula_that_ignored_the_merchant() -> None:
    # Arrange
    first_cut = hmac.new(KEY.encode(), f"card_ref:v1:{FINGERPRINT}".encode(), hashlib.sha256).hexdigest()[:32]

    # Act
    card_ref = derive_card_ref(FINGERPRINT, MERCHANT, KEY)

    # Assert
    assert card_ref == _expected(FINGERPRINT, MERCHANT, KEY)
    assert card_ref != first_cut


def test_derive_card_ref_gives_every_test_pan_a_distinct_value_at_one_merchant() -> None:
    # Arrange
    fingerprints = [card_fingerprint(pan, KEY) for pan in SORTED_PANS]

    # Act
    card_refs = [derive_card_ref(fingerprint, MERCHANT, KEY) for fingerprint in fingerprints]

    # Assert
    assert len(set(card_refs)) == len(SORTED_PANS)
