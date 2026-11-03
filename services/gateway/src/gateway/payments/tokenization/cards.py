import hashlib
import hmac
import uuid

TEST_PANS: frozenset[str] = frozenset(
    {
        "4242424242424242",
        "5555555555554444",
        "4000000000009995",
        "4000000000000002",
        "4000000000000119",
    }
)
"""Allowlist: only these test PANs are accepted anywhere in the system."""


def luhn_valid(pan: str) -> bool:
    if not (pan.isascii() and pan.isdigit()) or not 12 <= len(pan) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(pan)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def card_fingerprint(pan: str, key: str) -> str:
    """HMAC-SHA256(key, pan) hex. Keyed so the fingerprint cannot be brute-forced back to a PAN."""
    return hmac.new(key.encode(), pan.encode(), hashlib.sha256).hexdigest()


CARD_REF_SUBKEY_LABEL = b"card_ref-key:v1"


def derive_card_ref(fingerprint: str, merchant_id: uuid.UUID, key: str) -> str:
    """HMAC-SHA256(subkey, f"{merchant_id}:{fingerprint}") hex, first 32 characters, where
    subkey = HMAC-SHA256(key, "card_ref-key:v1").

    The card identity on the ops port: ops-ai can count distinct cards without holding the fingerprint.
    The subkey keeps card_ref apart from the fingerprint HMAC; the merchant id keeps it from linking a card
    across merchants.
    """
    subkey = hmac.new(key.encode(), CARD_REF_SUBKEY_LABEL, hashlib.sha256).digest()
    return hmac.new(subkey, f"{merchant_id}:{fingerprint}".encode(), hashlib.sha256).hexdigest()[:32]
