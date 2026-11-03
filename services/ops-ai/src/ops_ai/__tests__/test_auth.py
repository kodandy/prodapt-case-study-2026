"""Tests for ops_ai/auth.py, written from docs/specs/runtime.md (R4)."""

import base64
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from ops_ai.auth import InvalidTokenError, verify_merchant_token
from ops_ai.scope import Scope

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ISSUED_AT = int(NOW.timestamp())
EXPIRES_AT = ISSUED_AT + 900
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT_ID = uuid.UUID("7a9d3c1e-52f4-4b88-a0d6-1e2f3a4b5c6d")


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


_MISSING = object()


def _claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{MERCHANT_ID}",
        "merchant_id": str(MERCHANT_ID),
        "roles": ["merchant_admin"],
        "iat": ISSUED_AT,
        "exp": EXPIRES_AT,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not _MISSING}


def _sign(claims: dict[str, object], private_pem: str, algorithm: str = "RS256") -> str:
    return jwt.encode(claims, private_pem, algorithm=algorithm)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _json_b64url(value: dict[str, object]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


def _alg_none_token() -> str:
    return f"{_json_b64url({'alg': 'none', 'typ': 'JWT'})}.{_json_b64url(_claims())}."


def _hs256_with_public_key_token(public_pem: str) -> str:
    signing_input = f"{_json_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_json_b64url(_claims())}"
    signature = hmac.new(public_pem.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _payload_edited_token(valid_token: str) -> str:
    header, _, signature = valid_token.split(".")
    edited_payload = _json_b64url(_claims(merchant_id=str(OTHER_MERCHANT_ID)))
    return f"{header}.{edited_payload}.{signature}"


def _fragments(token: str) -> list[str]:
    return [token, *(part for part in token.split(".") if len(part) >= 10)]


def test_verify_merchant_token_returns_the_scope_of_the_token_merchant(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_claims(), key_pair.private_pem)

    # Act
    scope = verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert scope == Scope(merchant_id=MERCHANT_ID)


def test_verify_merchant_token_uses_the_injected_now_and_not_the_wall_clock(key_pair: KeyPair) -> None:
    # Arrange
    decade_ago = NOW - timedelta(days=3650)
    token = _sign(
        _claims(iat=int(decade_ago.timestamp()), exp=int(decade_ago.timestamp()) + 900), key_pair.private_pem
    )

    # Act
    scope = verify_merchant_token(token, key_pair.public_pem, decade_ago + timedelta(seconds=10))

    # Assert
    assert scope == Scope(merchant_id=MERCHANT_ID)


def test_verify_merchant_token_accepts_a_token_one_second_before_it_expires(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_claims(), key_pair.private_pem)

    # Act
    scope = verify_merchant_token(token, key_pair.public_pem, datetime.fromtimestamp(EXPIRES_AT - 1, UTC))

    # Assert
    assert scope == Scope(merchant_id=MERCHANT_ID)


def test_verify_merchant_token_rejects_a_token_one_second_after_it_expired(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_claims(), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_merchant_token(token, key_pair.public_pem, datetime.fromtimestamp(EXPIRES_AT + 1, UTC))

    # Assert
    assert exc.type is InvalidTokenError


def _build_expired(pair: KeyPair, _other: KeyPair) -> str:
    return _sign(_claims(iat=ISSUED_AT - 1800, exp=ISSUED_AT - 900), pair.private_pem)


INVALID_TOKEN_BUILDERS: dict[str, Callable[[KeyPair, KeyPair], str]] = {
    "expired": _build_expired,
    "wrong_audience": lambda pair, _: _sign(_claims(aud="billing"), pair.private_pem),
    "wrong_issuer": lambda pair, _: _sign(_claims(iss="evil-issuer"), pair.private_pem),
    "missing_audience": lambda pair, _: _sign(_claims(aud=_MISSING), pair.private_pem),
    "missing_issuer": lambda pair, _: _sign(_claims(iss=_MISSING), pair.private_pem),
    "missing_exp": lambda pair, _: _sign(_claims(exp=_MISSING), pair.private_pem),
    "missing_merchant_id": lambda pair, _: _sign(_claims(merchant_id=_MISSING), pair.private_pem),
    "non_uuid_merchant_id": lambda pair, _: _sign(_claims(merchant_id="not-a-uuid"), pair.private_pem),
    "empty_merchant_id": lambda pair, _: _sign(_claims(merchant_id=""), pair.private_pem),
    "numeric_merchant_id": lambda pair, _: _sign(_claims(merchant_id=12345), pair.private_pem),
    "signed_by_another_key_pair": lambda _, other: _sign(_claims(), other.private_pem),
    "rs512_instead_of_rs256": lambda pair, _: _sign(_claims(), pair.private_pem, algorithm="RS512"),
    "alg_none": lambda _pair, _other: _alg_none_token(),
    "hs256_with_public_key_as_secret": lambda pair, _: _hs256_with_public_key_token(pair.public_pem),
    "payload_edited_without_resigning": lambda pair, _: _payload_edited_token(
        _sign(_claims(), pair.private_pem)
    ),
    "garbage_text": lambda _pair, _other: "not-a-jwt-just-garbage-text",
    "empty_string": lambda _pair, _other: "",
    "three_empty_segments": lambda _pair, _other: "..",
    "three_non_base64_segments": lambda _pair, _other: "aaaaaaaaaa.bbbbbbbbbb.cccccccccc",
}


@pytest.mark.parametrize("case", list(INVALID_TOKEN_BUILDERS), ids=list(INVALID_TOKEN_BUILDERS))
def test_verify_merchant_token_rejects_an_invalid_token(
    key_pair: KeyPair, other_key_pair: KeyPair, case: str
) -> None:
    # Arrange
    token = INVALID_TOKEN_BUILDERS[case](key_pair, other_key_pair)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


@pytest.mark.parametrize("case", list(INVALID_TOKEN_BUILDERS), ids=list(INVALID_TOKEN_BUILDERS))
def test_verify_merchant_token_error_message_never_contains_the_token(
    key_pair: KeyPair, other_key_pair: KeyPair, case: str
) -> None:
    # Arrange
    token = INVALID_TOKEN_BUILDERS[case](key_pair, other_key_pair)
    fragments = [fragment for fragment in _fragments(token) if fragment]

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    message = f"{exc.value} {exc.value!r}"
    assert [fragment for fragment in fragments if fragment in message] == []
