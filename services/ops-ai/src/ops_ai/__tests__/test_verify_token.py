"""Tests for ops_ai.auth.verify_token and UserScope, written from docs/specs/sq2.md section 2."""

import base64
import dataclasses
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from ops_ai.auth import InvalidTokenError, verify_merchant_token, verify_token
from ops_ai.scope import Scope, UserScope

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ISSUED_AT = int(NOW.timestamp())
EXPIRES_AT = ISSUED_AT + 900
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
USER_ID = uuid.UUID("5d2c8e41-7a39-4b60-8f12-3c4d5e6f7a88")
OTHER_USER_ID = uuid.UUID("9e8d7c6b-5a49-4382-b1a0-0f1e2d3c4b5a")

_MISSING = object()


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


def _user_claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"user:{USER_ID}",
        "user_id": str(USER_ID),
        "roles": ["consumer"],
        "iat": ISSUED_AT,
        "exp": EXPIRES_AT,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not _MISSING}


def _merchant_claims(**overrides: object) -> dict[str, object]:
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


def _alg_none_user_token() -> str:
    return f"{_json_b64url({'alg': 'none', 'typ': 'JWT'})}.{_json_b64url(_user_claims())}."


def _hs256_user_token(public_pem: str) -> str:
    signing_input = f"{_json_b64url({'alg': 'HS256', 'typ': 'JWT'})}.{_json_b64url(_user_claims())}"
    signature = hmac.new(public_pem.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


def _payload_edited_user_token(valid_token: str) -> str:
    header, _, signature = valid_token.split(".")
    edited = _json_b64url(_user_claims(user_id=str(OTHER_USER_ID), sub=f"user:{OTHER_USER_ID}"))
    return f"{header}.{edited}.{signature}"


# --- UserScope -------------------------------------------------------------------------------------------


def test_user_scope_carries_the_user_id_and_equals_a_scope_of_the_same_id_only_to_itself() -> None:
    # Arrange / Act
    scope = UserScope(user_id=USER_ID)

    # Assert
    assert scope.user_id == USER_ID
    assert scope == UserScope(user_id=USER_ID)
    assert scope != UserScope(user_id=OTHER_USER_ID)
    assert cast(object, scope) != Scope(merchant_id=USER_ID)


def test_user_scope_is_frozen() -> None:
    # Arrange
    scope = UserScope(user_id=USER_ID)

    # Act
    with pytest.raises(dataclasses.FrozenInstanceError) as exc:
        scope.user_id = OTHER_USER_ID  # type: ignore[misc]

    # Assert
    assert exc.type is dataclasses.FrozenInstanceError


# --- accepted tokens -------------------------------------------------------------------------------------


def test_verify_token_returns_a_merchant_scope_for_a_merchant_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == Scope(merchant_id=MERCHANT_ID)
    assert type(principal) is Scope


def test_verify_token_returns_a_user_scope_for_a_user_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_user_claims(), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == UserScope(user_id=USER_ID)
    assert type(principal) is UserScope


def test_verify_token_uses_the_injected_now_for_a_user_token(key_pair: KeyPair) -> None:
    # Arrange
    decade_ago = NOW - timedelta(days=3650)
    token = _sign(
        _user_claims(iat=int(decade_ago.timestamp()), exp=int(decade_ago.timestamp()) + 900),
        key_pair.private_pem,
    )

    # Act
    principal = verify_token(token, key_pair.public_pem, decade_ago + timedelta(seconds=10))

    # Assert
    assert principal == UserScope(user_id=USER_ID)


def test_verify_token_accepts_a_user_token_one_second_before_it_expires(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_user_claims(), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, datetime.fromtimestamp(EXPIRES_AT - 1, UTC))

    # Assert
    assert principal == UserScope(user_id=USER_ID)


def test_verify_token_rejects_a_user_token_at_the_second_it_expires(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_user_claims(), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, datetime.fromtimestamp(EXPIRES_AT, UTC))

    # Assert
    assert exc.type is InvalidTokenError


# --- claim rules -----------------------------------------------------------------------------------------

CLAIM_CASES: dict[str, Callable[[], dict[str, object]]] = {
    "both_merchant_id_and_user_id": lambda: _user_claims(merchant_id=str(MERCHANT_ID)),
    "both_claims_with_merchant_role": lambda: _merchant_claims(user_id=str(USER_ID)),
    "neither_claim_on_a_user_shape": lambda: _user_claims(user_id=_MISSING),
    "neither_claim_on_a_merchant_shape": lambda: _merchant_claims(merchant_id=_MISSING),
    "empty_user_id": lambda: _user_claims(user_id=""),
    "non_uuid_user_id": lambda: _user_claims(user_id="not-a-uuid"),
    "numeric_user_id": lambda: _user_claims(user_id=12345),
    "null_user_id": lambda: _user_claims(user_id=None),
    "list_user_id": lambda: _user_claims(user_id=[str(USER_ID)]),
    "empty_merchant_id": lambda: _merchant_claims(merchant_id=""),
    "non_uuid_merchant_id": lambda: _merchant_claims(merchant_id="not-a-uuid"),
    "numeric_merchant_id": lambda: _merchant_claims(merchant_id=12345),
    "user_token_without_roles": lambda: _user_claims(roles=_MISSING),
    "merchant_token_without_roles": lambda: _merchant_claims(roles=_MISSING),
    "user_token_with_merchant_role": lambda: _user_claims(roles=["merchant_admin"]),
    "merchant_token_with_consumer_role": lambda: _merchant_claims(roles=["consumer"]),
    "user_token_with_extra_role": lambda: _user_claims(roles=["consumer", "merchant_admin"]),
    "merchant_token_with_extra_role": lambda: _merchant_claims(roles=["merchant_admin", "consumer"]),
    "user_token_with_duplicated_role": lambda: _user_claims(roles=["consumer", "consumer"]),
    "user_token_with_empty_roles": lambda: _user_claims(roles=[]),
    "merchant_token_with_empty_roles": lambda: _merchant_claims(roles=[]),
    "user_token_with_unknown_role": lambda: _user_claims(roles=["admin"]),
    "user_token_with_role_as_a_string": lambda: _user_claims(roles="consumer"),
    "merchant_token_with_role_as_a_string": lambda: _merchant_claims(roles="merchant_admin"),
    "user_token_with_wrong_case_role": lambda: _user_claims(roles=["Consumer"]),
    "user_token_with_roles_null": lambda: _user_claims(roles=None),
    "user_sub_of_another_user": lambda: _user_claims(sub=f"user:{OTHER_USER_ID}"),
    "user_sub_of_a_merchant_shape": lambda: _user_claims(sub=f"merchant:{USER_ID}"),
    "user_sub_without_prefix": lambda: _user_claims(sub=str(USER_ID)),
    "user_sub_missing": lambda: _user_claims(sub=_MISSING),
    "user_sub_empty": lambda: _user_claims(sub=""),
}


@pytest.mark.parametrize("case", list(CLAIM_CASES), ids=list(CLAIM_CASES))
def test_verify_token_rejects_a_token_that_breaks_a_claim_rule(key_pair: KeyPair, case: str) -> None:
    # Arrange
    token = _sign(CLAIM_CASES[case](), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


# --- the checks verify_merchant_token already made, now on user tokens ------------------------------------

USER_TOKEN_BUILDERS: dict[str, Callable[[KeyPair, KeyPair], str]] = {
    "expired": lambda pair, _: _sign(
        _user_claims(iat=ISSUED_AT - 1800, exp=ISSUED_AT - 900), pair.private_pem
    ),
    "missing_exp": lambda pair, _: _sign(_user_claims(exp=_MISSING), pair.private_pem),
    "wrong_audience": lambda pair, _: _sign(_user_claims(aud="billing"), pair.private_pem),
    "wrong_issuer": lambda pair, _: _sign(_user_claims(iss="evil-issuer"), pair.private_pem),
    "missing_audience": lambda pair, _: _sign(_user_claims(aud=_MISSING), pair.private_pem),
    "missing_issuer": lambda pair, _: _sign(_user_claims(iss=_MISSING), pair.private_pem),
    "signed_by_another_key_pair": lambda _, other: _sign(_user_claims(), other.private_pem),
    "rs512_instead_of_rs256": lambda pair, _: _sign(_user_claims(), pair.private_pem, algorithm="RS512"),
    "alg_none": lambda _pair, _other: _alg_none_user_token(),
    "hs256_with_public_key_as_secret": lambda pair, _: _hs256_user_token(pair.public_pem),
    "payload_edited_without_resigning": lambda pair, _: _payload_edited_user_token(
        _sign(_user_claims(), pair.private_pem)
    ),
    "garbage_text": lambda _pair, _other: "not-a-jwt-just-garbage-text",
    "empty_string": lambda _pair, _other: "",
}


@pytest.mark.parametrize("case", list(USER_TOKEN_BUILDERS), ids=list(USER_TOKEN_BUILDERS))
def test_verify_token_rejects_an_invalid_user_token(
    key_pair: KeyPair, other_key_pair: KeyPair, case: str
) -> None:
    # Arrange
    token = USER_TOKEN_BUILDERS[case](key_pair, other_key_pair)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


@pytest.mark.parametrize("case", list(USER_TOKEN_BUILDERS), ids=list(USER_TOKEN_BUILDERS))
def test_verify_token_error_message_never_contains_a_user_token(
    key_pair: KeyPair, other_key_pair: KeyPair, case: str
) -> None:
    # Arrange
    token = USER_TOKEN_BUILDERS[case](key_pair, other_key_pair)
    fragments = [fragment for fragment in [token, *token.split(".")] if len(fragment) >= 10]

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    message = f"{exc.value} {exc.value!r}"
    assert [fragment for fragment in fragments if fragment in message] == []


# --- verify_merchant_token only accepts merchant scopes --------------------------------------------------


def test_verify_merchant_token_rejects_a_valid_user_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_user_claims(), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


def test_verify_merchant_token_still_returns_the_scope_of_a_merchant_token(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(), key_pair.private_pem)

    # Act
    scope = verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert scope == Scope(merchant_id=MERCHANT_ID)


def test_verify_merchant_token_rejects_a_merchant_token_with_the_consumer_role(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(roles=["consumer"]), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError
