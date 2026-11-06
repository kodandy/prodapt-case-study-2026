"""R2-7: a merchant token may name the gateway API key that obtained it (`api_key_id`)."""

import uuid
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from ops_ai.auth import InvalidTokenError, verify_merchant_token, verify_token
from ops_ai.scope import Scope

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
ISSUED_AT = int(NOW.timestamp())
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
API_KEY_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9cff")
USER_ID = uuid.UUID("5d2c8e41-7a39-4b60-8f12-3c4d5e6f7a88")


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _public_pem(private_pem: str) -> str:
    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _merchant_token(private_pem: str, **extra: object) -> str:
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT_ID}",
        "merchant_id": str(MERCHANT_ID),
        "roles": ["merchant_support"],
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
        "jti": str(uuid.uuid4()),
    }
    claims.update(extra)
    return jwt.encode(claims, private_pem, algorithm="RS256")


def test_a_merchant_token_with_an_api_key_id_yields_a_scope_carrying_it(private_pem: str) -> None:
    # Arrange
    token = _merchant_token(private_pem, api_key_id=str(API_KEY_ID))

    # Act
    scope = verify_merchant_token(token, _public_pem(private_pem), NOW)

    # Assert
    assert scope == Scope(merchant_id=MERCHANT_ID, role="merchant_support", api_key_id=API_KEY_ID)


def test_a_merchant_token_without_an_api_key_id_yields_a_scope_without_one(private_pem: str) -> None:
    # Arrange
    token = _merchant_token(private_pem)

    # Act
    scope = verify_merchant_token(token, _public_pem(private_pem), NOW)

    # Assert
    assert scope.api_key_id is None
    assert scope == Scope(merchant_id=MERCHANT_ID, role="merchant_support")


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        str(API_KEY_ID).upper(),
        str(API_KEY_ID).replace("-", ""),
        42,
        None,
        [str(API_KEY_ID)],
    ],
    ids=["empty", "garbage", "uppercase", "unhyphenated", "integer", "null", "list"],
)
def test_a_merchant_token_with_a_malformed_api_key_id_is_rejected(private_pem: str, value: object) -> None:
    # Arrange
    token = _merchant_token(private_pem, api_key_id=value)

    # Act
    with pytest.raises(InvalidTokenError) as raised:
        verify_token(token, _public_pem(private_pem), NOW)

    # Assert
    assert str(raised.value) == "invalid api_key_id claim"


def test_a_user_token_carrying_an_api_key_id_is_rejected(private_pem: str) -> None:
    # Arrange
    token = jwt.encode(
        {
            "iss": "gateway",
            "aud": "ops-ai",
            "sub": f"user:{USER_ID}",
            "user_id": str(USER_ID),
            "roles": ["consumer"],
            "iat": ISSUED_AT,
            "exp": ISSUED_AT + 900,
            "api_key_id": str(API_KEY_ID),
        },
        private_pem,
        algorithm="RS256",
    )

    # Act
    with pytest.raises(InvalidTokenError) as raised:
        verify_token(token, _public_pem(private_pem), NOW)

    # Assert
    assert str(raised.value) == "api_key_id is only valid on a merchant token"
