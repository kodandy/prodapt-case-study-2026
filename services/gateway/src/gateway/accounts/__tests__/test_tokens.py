"""Tests for gateway/accounts/tokens.py, written from docs/specs/runtime.md (R1)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from gateway.accounts.tokens import (
    MERCHANT_TOKEN_TTL_SECONDS,
    TOKEN_ALGORITHM,
    TOKEN_AUDIENCE,
    TOKEN_ISSUER,
    issue_merchant_token,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


def _generate_key_pair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private_pem=private_pem, public_pem=public_pem)


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    return _generate_key_pair()


@pytest.fixture(scope="module")
def other_key_pair() -> KeyPair:
    return _generate_key_pair()


def _unverified_claims(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def test_token_constants_match_the_spec() -> None:
    # Arrange / Act
    constants = (TOKEN_ISSUER, TOKEN_AUDIENCE, TOKEN_ALGORITHM, MERCHANT_TOKEN_TTL_SECONDS)

    # Assert
    assert constants == ("gateway", "ops-ai", "RS256", 900)


def test_issue_merchant_token_carries_the_expected_claims(key_pair: KeyPair) -> None:
    # Arrange
    issued_at = int(NOW.timestamp())

    # Act
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW)

    # Assert
    claims = _unverified_claims(token)
    assert claims == {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT_ID}",
        "merchant_id": str(MERCHANT_ID),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": claims.get("jti"),
    }


def test_issue_merchant_token_header_algorithm_is_rs256(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW)

    # Assert
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_issue_merchant_token_jti_is_a_uuid(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW)

    # Assert
    jti = str(_unverified_claims(token)["jti"])
    assert str(uuid.UUID(jti)) == jti


def test_issue_merchant_token_uses_a_new_jti_on_each_call(key_pair: KeyPair) -> None:
    # Arrange / Act
    first = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW)
    second = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW)

    # Assert
    assert _unverified_claims(first)["jti"] != _unverified_claims(second)["jti"]
    assert first != second


@pytest.mark.parametrize("ttl_seconds", [1, 60, 3600])
def test_issue_merchant_token_exp_is_iat_plus_the_given_ttl(key_pair: KeyPair, ttl_seconds: int) -> None:
    # Arrange / Act
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW, ttl_seconds=ttl_seconds)

    # Assert
    claims = _unverified_claims(token)
    assert claims["iat"] == int(NOW.timestamp())
    assert claims["exp"] == int(NOW.timestamp()) + ttl_seconds


def test_issue_merchant_token_verifies_with_the_matching_public_key(key_pair: KeyPair) -> None:
    # Arrange
    now = datetime.now(UTC)
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, now)

    # Act
    claims = jwt.decode(
        token, key_pair.public_pem, algorithms=["RS256"], audience=TOKEN_AUDIENCE, issuer=TOKEN_ISSUER
    )

    # Assert
    assert claims["merchant_id"] == str(MERCHANT_ID)
    assert claims["sub"] == f"merchant:{MERCHANT_ID}"


def test_issue_merchant_token_fails_verification_with_a_different_key_pair(
    key_pair: KeyPair, other_key_pair: KeyPair
) -> None:
    # Arrange
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, datetime.now(UTC))

    # Act
    with pytest.raises(jwt.InvalidSignatureError) as exc:
        jwt.decode(
            token,
            other_key_pair.public_pem,
            algorithms=["RS256"],
            audience=TOKEN_AUDIENCE,
            issuer=TOKEN_ISSUER,
        )

    # Assert
    assert exc.type is jwt.InvalidSignatureError
