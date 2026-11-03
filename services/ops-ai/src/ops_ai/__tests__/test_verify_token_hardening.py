"""Tests for the stricter verify_token checks, written from docs/specs/sq2.md section 9.3 (audit L1, L2)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from ops_ai.auth import InvalidTokenError, verify_merchant_token, verify_token
from ops_ai.scope import Scope, UserScope

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ISSUED_AT = int(NOW.timestamp())
MERCHANT_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
USER_ID = uuid.UUID("5d2c8e41-7a39-4b60-8f12-3c4d5e6f7a88")

_MISSING = object()

ClaimsBuilder = Callable[..., dict[str, object]]


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
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


def _user_claims(**overrides: object) -> dict[str, object]:
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"user:{USER_ID}",
        "user_id": str(USER_ID),
        "roles": ["consumer"],
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
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
        "exp": ISSUED_AT + 900,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return {name: value for name, value in claims.items() if value is not _MISSING}


def _sign(claims: dict[str, object], private_pem: str) -> str:
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _underscored(value: uuid.UUID) -> str:
    """32 characters, so uuid.UUID() parses it today, with underscores in place of four hex digits."""
    h = value.hex
    return f"{h[:8]}_{h[9:12]}_{h[13:16]}_{h[17:20]}_{h[21:]}"


KINDS: list[tuple[str, ClaimsBuilder, str, uuid.UUID, type[Scope] | type[UserScope]]] = [
    ("merchant", _merchant_claims, "merchant_id", MERCHANT_ID, Scope),
    ("user", _user_claims, "user_id", USER_ID, UserScope),
]
KIND_IDS = [kind[0] for kind in KINDS]


# --- iat is required and must be an int ------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_rejects_a_token_without_iat(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    token = _sign(claims_of(iat=_MISSING), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_rejects_a_boolean_iat(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    token = _sign(claims_of(iat=True), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


@pytest.mark.parametrize(
    "bad_iat", [str(ISSUED_AT), float(ISSUED_AT) + 0.5, None], ids=["str", "float", "null"]
)
@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_rejects_an_iat_that_is_not_an_int(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
    bad_iat: object,
) -> None:
    # Arrange
    token = _sign(claims_of(iat=bad_iat), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


# --- iat in the future -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_rejects_an_iat_31_seconds_in_the_future(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    future_iat = ISSUED_AT + 31
    token = _sign(claims_of(iat=future_iat, exp=future_iat + 900), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError, match="token issued in the future") as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_accepts_an_iat_exactly_30_seconds_in_the_future(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    skewed_iat = ISSUED_AT + 30
    token = _sign(claims_of(iat=skewed_iat, exp=skewed_iat + 900), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == (
        Scope(merchant_id=identity) if id_claim == "merchant_id" else UserScope(user_id=identity)
    )


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_accepts_an_iat_in_the_past_within_the_lifetime(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    token = _sign(claims_of(iat=ISSUED_AT - 600, exp=ISSUED_AT + 300), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert type(principal) is scope_type


# --- lifetime --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lifetime", [901, 3600, 86_400], ids=["901s", "1h", "1d"])
@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_rejects_a_lifetime_over_900_seconds(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
    lifetime: int,
) -> None:
    # Arrange
    token = _sign(claims_of(iat=ISSUED_AT, exp=ISSUED_AT + lifetime), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError, match="token lifetime too long") as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_accepts_a_lifetime_of_exactly_900_seconds(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    token = _sign(claims_of(iat=ISSUED_AT, exp=ISSUED_AT + 900), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == (
        Scope(merchant_id=identity) if id_claim == "merchant_id" else UserScope(user_id=identity)
    )


def test_verify_merchant_token_also_rejects_a_lifetime_over_900_seconds(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(iat=ISSUED_AT, exp=ISSUED_AT + 901), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError, match="token lifetime too long") as exc:
        verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


def test_verify_merchant_token_also_rejects_a_token_without_iat(key_pair: KeyPair) -> None:
    # Arrange
    token = _sign(_merchant_claims(iat=_MISSING), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError) as exc:
        verify_merchant_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


# --- canonical UUID strings only -------------------------------------------------------------------------


def _non_canonical_forms(identity: uuid.UUID) -> dict[str, str]:
    return {
        "uppercase": str(identity).upper(),
        "braces": "{" + str(identity) + "}",
        "urn_prefix": "urn:uuid:" + str(identity),
        "no_hyphens": identity.hex,
        "underscores": _underscored(identity),
    }


@pytest.mark.parametrize("form", ["uppercase", "braces", "urn_prefix", "no_hyphens", "underscores"])
@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_rejects_a_non_canonical_uuid_claim(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
    form: str,
) -> None:
    # Arrange
    token = _sign(claims_of(**{id_claim: _non_canonical_forms(identity)[form]}), key_pair.private_pem)

    # Act
    with pytest.raises(InvalidTokenError, match=f"invalid {id_claim} claim") as exc:
        verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert exc.type is InvalidTokenError


def test_the_underscored_form_is_one_that_uuid_parsing_alone_would_accept() -> None:
    # Arrange
    form = _underscored(USER_ID)

    # Act
    parsed = uuid.UUID(form)

    # Assert
    assert len(form) == 32
    assert str(parsed) != form


@pytest.mark.parametrize(("kind", "claims_of", "id_claim", "identity", "scope_type"), KINDS, ids=KIND_IDS)
def test_verify_token_still_accepts_the_canonical_lowercase_uuid(
    key_pair: KeyPair,
    kind: str,
    claims_of: ClaimsBuilder,
    id_claim: str,
    identity: uuid.UUID,
    scope_type: type[Scope] | type[UserScope],
) -> None:
    # Arrange
    token = _sign(claims_of(**{id_claim: str(identity)}), key_pair.private_pem)

    # Act
    principal = verify_token(token, key_pair.public_pem, NOW)

    # Assert
    assert principal == (
        Scope(merchant_id=identity) if id_claim == "merchant_id" else UserScope(user_id=identity)
    )
