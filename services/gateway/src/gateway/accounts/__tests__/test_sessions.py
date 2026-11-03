"""Tests for gateway/accounts/sessions.py, written from docs/specs/gateway-basic.md section B1."""

import base64
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from gateway.accounts.sessions import InvalidSessionError, issue_session_token, verify_session_token
from gateway.accounts.tokens import issue_merchant_token, issue_user_token

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c77")
OTHER_USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c78")
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


def _claims(
    user_id: uuid.UUID = USER_ID, amr: tuple[str, ...] = ("pwd",), **overrides: object
) -> dict[str, object]:
    issued_at = int(NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "gateway",
        "sub": f"user:{user_id}",
        "user_id": str(user_id),
        "amr": list(amr),
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return claims


def _without(claims: dict[str, object], *names: str) -> dict[str, object]:
    return {name: value for name, value in claims.items() if name not in names}


def _signed(claims: dict[str, object], private_pem: str) -> str:
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _unverified_claims(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _hs256_signed_with(secret: bytes, claims: dict[str, object]) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps(claims).encode())
    signature = hmac.new(secret, f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(signature)}"


# --- issue_session_token ---------------------------------------------------------------------------------


def test_issue_session_token_carries_exactly_the_specified_claims(key_pair: KeyPair) -> None:
    # Arrange
    issued_at = int(NOW.timestamp())

    # Act
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",))

    # Assert
    claims = _unverified_claims(token)
    assert claims == {
        "iss": "gateway",
        "aud": "gateway",
        "sub": f"user:{USER_ID}",
        "user_id": str(USER_ID),
        "amr": ["pwd"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": claims.get("jti"),
    }


def test_issue_session_token_records_both_factors_in_amr(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd", "otp"))

    # Assert
    assert _unverified_claims(token)["amr"] == ["pwd", "otp"]


def test_issue_session_token_expires_900_seconds_after_it_is_issued(key_pair: KeyPair) -> None:
    # Arrange / Act
    claims = _unverified_claims(issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",)))

    # Assert
    assert (claims["iat"], claims["exp"]) == (int(NOW.timestamp()), int(NOW.timestamp()) + 900)


def test_issue_session_token_header_algorithm_is_rs256(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",))

    # Assert
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_issue_session_token_verifies_with_the_public_key_issuer_and_audience(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_session_token(key_pair.private_pem, USER_ID, datetime.now(UTC), ("pwd",))

    # Act
    claims = jwt.decode(
        token, key_pair.public_pem, algorithms=["RS256"], audience="gateway", issuer="gateway"
    )

    # Assert
    assert claims["user_id"] == str(USER_ID)


def test_issue_session_token_uses_a_new_uuid_jti_on_each_call(key_pair: KeyPair) -> None:
    # Arrange / Act
    first = str(_unverified_claims(issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",)))["jti"])
    second = str(_unverified_claims(issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",)))["jti"])

    # Assert
    assert first != second
    assert str(uuid.UUID(first)) == first
    assert str(uuid.UUID(second)) == second


# --- verify_session_token: accepted ----------------------------------------------------------------------


def test_verify_session_token_returns_the_user_id_and_amr_of_a_fresh_token(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd", "otp"))

    # Act
    claims = verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert (claims.user_id, claims.amr) == (USER_ID, ("pwd", "otp"))


def test_verify_session_token_returns_a_password_only_amr_as_a_tuple(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",))

    # Act
    claims = verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert claims.amr == ("pwd",)


def test_verify_session_token_still_accepts_a_token_one_second_before_it_expires(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",))

    # Act
    claims = verify_session_token(key_pair.private_pem, token, NOW + timedelta(seconds=899))

    # Assert
    assert claims.user_id == USER_ID


# --- verify_session_token: rejected ----------------------------------------------------------------------


def test_verify_session_token_rejects_an_expired_token(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",))

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW + timedelta(seconds=901))

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_an_ops_ai_user_token(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_user_token(key_pair.private_pem, USER_ID, NOW)

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_a_merchant_token(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_merchant_token(key_pair.private_pem, MERCHANT_ID, NOW)

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_a_token_with_another_issuer(key_pair: KeyPair) -> None:
    # Arrange
    token = _signed(_claims(iss="someone-else"), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_a_token_signed_by_another_key(
    key_pair: KeyPair, other_key_pair: KeyPair
) -> None:
    # Arrange
    token = issue_session_token(other_key_pair.private_pem, USER_ID, NOW, ("pwd",))

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_a_payload_swapped_under_the_original_signature(
    key_pair: KeyPair,
) -> None:
    # Arrange
    genuine = issue_session_token(key_pair.private_pem, USER_ID, NOW, ("pwd",))
    forged_payload = _b64url(json.dumps(_claims(user_id=OTHER_USER_ID, amr=("pwd", "otp"))).encode())
    header, _, signature = genuine.split(".")
    tampered = f"{header}.{forged_payload}.{signature}"

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, tampered, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_an_hs256_token_signed_with_an_arbitrary_secret(
    key_pair: KeyPair,
) -> None:
    # Arrange
    token = jwt.encode(_claims(), "an-arbitrary-shared-secret-32-bytes!", algorithm="HS256")

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_an_hs256_token_signed_with_the_public_key_bytes(
    key_pair: KeyPair,
) -> None:
    # Arrange
    token = _hs256_signed_with(key_pair.public_pem.encode(), _claims(amr=("pwd", "otp")))

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


def test_verify_session_token_rejects_an_unsigned_none_algorithm_token(key_pair: KeyPair) -> None:
    # Arrange
    token = jwt.encode(_claims(), key="", algorithm="none")

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


@pytest.mark.parametrize("missing", [("exp",), ("amr",), ("user_id", "sub")])
def test_verify_session_token_rejects_a_token_missing_required_claims(
    key_pair: KeyPair, missing: tuple[str, ...]
) -> None:
    # Arrange
    token = _signed(_without(_claims(), *missing), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert exc.type is InvalidSessionError


@pytest.mark.parametrize("garbage", ["", "not-a-token", "a.b.c", "....", "Bearer abc"])
def test_verify_session_token_rejects_garbage(key_pair: KeyPair, garbage: str) -> None:
    # Arrange / Act
    with pytest.raises(InvalidSessionError) as exc:
        verify_session_token(key_pair.private_pem, garbage, NOW)

    # Assert
    assert exc.type is InvalidSessionError
