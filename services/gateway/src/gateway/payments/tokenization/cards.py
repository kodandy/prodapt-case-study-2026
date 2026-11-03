import hashlib
import hmac

TEST_PANS: frozenset[str] = frozenset(
    {
        "4242424242424242",
        "5555555555554444",
        "4000000000009995",
        "4000000000000002",
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
