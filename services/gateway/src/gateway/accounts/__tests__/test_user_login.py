"""Tests for user login in gateway/accounts, written from docs/specs/sq2.md section 1 (Gateway)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from gateway.accounts.service import (
    UnknownUserError,
    authenticate_user,
    set_password,
    sign_up,
    verify_password,
)
from gateway.accounts.tokens import (
    TOKEN_AUDIENCE,
    TOKEN_ISSUER,
    USER_TOKEN_TTL_SECONDS,
    issue_user_token,
)
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"
NEW_PASSWORD = "a brand new passphrase"
EMAIL = "alice@example.com"
USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c77")
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


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


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _signed_up(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL, password: str = PASSWORD
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, password)
        session.commit()
    return result.user_id


@pytest.fixture
def count_argon2_verifications(monkeypatch: pytest.MonkeyPatch) -> Callable[[], int]:
    """Counts every PasswordHasher.verify call, however the service reaches it."""
    calls: list[str] = []
    original = PasswordHasher.verify

    def counting(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        calls.append("verify")
        return original(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting)
    return lambda: len(calls)


# --- sign_up with a chosen user id -----------------------------------------------------------------------


def test_sign_up_with_a_user_id_creates_the_user_row_under_that_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = sign_up(session, clock, EMAIL, PASSWORD, user_id=USER_ID)
        session.commit()

    # Assert
    assert result.user_id == USER_ID
    assert _rows(session_factory, "SELECT id, email FROM gateway.users") == [{"id": USER_ID, "email": EMAIL}]


def test_sign_up_with_a_user_id_creates_the_wallet_account_for_that_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = sign_up(session, clock, EMAIL, PASSWORD, currency="USD", user_id=USER_ID)
        session.commit()

    # Assert
    assert _rows(session_factory, "SELECT id, owner_id, kind, currency FROM gateway.ledger_accounts") == [
        {"id": result.wallet_account_id, "owner_id": USER_ID, "kind": "wallet", "currency": "USD"}
    ]


def test_sign_up_without_a_user_id_still_generates_a_random_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        first = sign_up(session, clock, "first@example.com", PASSWORD)
        second = sign_up(session, clock, "second@example.com", PASSWORD)
        session.commit()

    # Assert
    assert first.user_id != second.user_id
    assert first.user_id.version == 4
    assert {row["id"] for row in _rows(session_factory, "SELECT id FROM gateway.users")} == {
        first.user_id,
        second.user_id,
    }


# --- authenticate_user -----------------------------------------------------------------------------------


def test_authenticate_user_returns_the_user_id_for_the_right_email_and_password(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        result = authenticate_user(session, EMAIL, PASSWORD)

    # Assert
    assert result == user_id


def test_authenticate_user_matches_the_email_case_insensitively(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock, email="Alice@Example.com")

    # Act
    with session_factory() as session:
        upper = authenticate_user(session, "ALICE@EXAMPLE.COM", PASSWORD)
        mixed = authenticate_user(session, "aLiCe@example.COM", PASSWORD)

    # Assert
    assert (upper, mixed) == (user_id, user_id)


def test_authenticate_user_returns_none_for_a_wrong_password(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        result = authenticate_user(session, EMAIL, "not the password")

    # Assert
    assert result is None


def test_authenticate_user_returns_none_for_an_unknown_email(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        result = authenticate_user(session, "nobody@example.com", PASSWORD)

    # Assert
    assert result is None


def test_authenticate_user_does_not_accept_a_password_that_only_prefixes_the_real_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        result = authenticate_user(session, EMAIL, PASSWORD[:-1])

    # Assert
    assert result is None


def test_authenticate_user_picks_the_user_of_the_matching_email_among_several(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _signed_up(session_factory, clock, email="alice@example.com", password=PASSWORD)
    bob = _signed_up(session_factory, clock, email="bob@example.com", password=NEW_PASSWORD)

    # Act
    with session_factory() as session:
        results = (
            authenticate_user(session, "alice@example.com", PASSWORD),
            authenticate_user(session, "bob@example.com", NEW_PASSWORD),
            authenticate_user(session, "alice@example.com", NEW_PASSWORD),
        )

    # Assert
    assert results == (alice, bob, None)


def test_authenticate_user_runs_one_argon2_verification_for_a_correct_login(
    session_factory: sessionmaker[Session], clock: FixedClock, count_argon2_verifications: Callable[[], int]
) -> None:
    # Arrange
    _signed_up(session_factory, clock)
    before = count_argon2_verifications()

    # Act
    with session_factory() as session:
        authenticate_user(session, EMAIL, PASSWORD)

    # Assert
    assert count_argon2_verifications() - before == 1


def test_authenticate_user_runs_one_argon2_verification_for_a_wrong_password(
    session_factory: sessionmaker[Session], clock: FixedClock, count_argon2_verifications: Callable[[], int]
) -> None:
    # Arrange
    _signed_up(session_factory, clock)
    before = count_argon2_verifications()

    # Act
    with session_factory() as session:
        authenticate_user(session, EMAIL, "not the password")

    # Assert
    assert count_argon2_verifications() - before == 1


def test_authenticate_user_runs_one_argon2_verification_for_an_unknown_email(
    session_factory: sessionmaker[Session], clock: FixedClock, count_argon2_verifications: Callable[[], int]
) -> None:
    # Arrange
    before = count_argon2_verifications()

    # Act
    with session_factory() as session:
        result = authenticate_user(session, "nobody@example.com", PASSWORD)

    # Assert
    assert result is None
    assert count_argon2_verifications() - before == 1


# --- set_password ----------------------------------------------------------------------------------------


def test_set_password_replaces_the_password_so_only_the_new_one_logs_in(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with session_factory.begin() as session:
        set_password(session, user_id, NEW_PASSWORD)

    # Assert
    with session_factory() as session:
        assert authenticate_user(session, EMAIL, PASSWORD) is None
        assert authenticate_user(session, EMAIL, NEW_PASSWORD) == user_id


def test_set_password_stores_an_argon2id_hash_and_never_the_plaintext(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with session_factory.begin() as session:
        set_password(session, user_id, NEW_PASSWORD)

    # Assert
    stored = str(_rows(session_factory, "SELECT password_hash FROM gateway.users")[0]["password_hash"])
    assert stored.startswith("$argon2id$")
    assert NEW_PASSWORD not in stored
    assert verify_password(stored, NEW_PASSWORD) is True


def test_set_password_does_not_commit(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        set_password(session, user_id, NEW_PASSWORD)
        session.rollback()

    # Assert
    with session_factory() as session:
        assert authenticate_user(session, EMAIL, PASSWORD) == user_id
        assert authenticate_user(session, EMAIL, NEW_PASSWORD) is None


def test_set_password_leaves_the_other_users_untouched(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _signed_up(session_factory, clock, email="alice@example.com")
    bob = _signed_up(session_factory, clock, email="bob@example.com")

    # Act
    with session_factory.begin() as session:
        set_password(session, alice, NEW_PASSWORD)

    # Assert
    with session_factory() as session:
        assert authenticate_user(session, "bob@example.com", PASSWORD) == bob


def test_set_password_raises_unknown_user_error_for_an_unknown_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock)

    # Act
    with session_factory() as session, pytest.raises(UnknownUserError) as exc:
        set_password(session, uuid.UUID("deadbeef-0000-4000-8000-000000000000"), NEW_PASSWORD)

    # Assert
    assert exc.type is UnknownUserError


# --- issue_user_token ------------------------------------------------------------------------------------


def _unverified_claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, options={"verify_signature": False})


def test_user_token_ttl_is_900_seconds() -> None:
    # Arrange / Act
    ttl = USER_TOKEN_TTL_SECONDS

    # Assert
    assert ttl == 900


def test_issue_user_token_carries_exactly_the_specified_claims(key_pair: KeyPair) -> None:
    # Arrange
    issued_at = int(NOW.timestamp())

    # Act
    token = issue_user_token(key_pair.private_pem, USER_ID, NOW)

    # Assert
    claims = _unverified_claims(token)
    assert claims == {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"user:{USER_ID}",
        "user_id": str(USER_ID),
        "roles": ["consumer"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": claims.get("jti"),
    }


def test_issue_user_token_has_no_merchant_id_claim(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_user_token(key_pair.private_pem, USER_ID, NOW)

    # Assert
    assert "merchant_id" not in _unverified_claims(token)


def test_issue_user_token_expires_900_seconds_after_it_is_issued(key_pair: KeyPair) -> None:
    # Arrange / Act
    claims = _unverified_claims(issue_user_token(key_pair.private_pem, USER_ID, NOW))

    # Assert
    assert claims["exp"] - claims["iat"] == 900


@pytest.mark.parametrize("ttl_seconds", [1, 60, 3600])
def test_issue_user_token_exp_is_iat_plus_the_given_ttl(key_pair: KeyPair, ttl_seconds: int) -> None:
    # Arrange / Act
    claims = _unverified_claims(issue_user_token(key_pair.private_pem, USER_ID, NOW, ttl_seconds=ttl_seconds))

    # Assert
    assert (claims["iat"], claims["exp"]) == (int(NOW.timestamp()), int(NOW.timestamp()) + ttl_seconds)


def test_issue_user_token_header_algorithm_is_rs256(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_user_token(key_pair.private_pem, USER_ID, NOW)

    # Assert
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_issue_user_token_verifies_with_the_public_key_issuer_and_audience(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_user_token(key_pair.private_pem, USER_ID, datetime.now(UTC))

    # Act
    claims = jwt.decode(
        token, key_pair.public_pem, algorithms=["RS256"], audience=TOKEN_AUDIENCE, issuer=TOKEN_ISSUER
    )

    # Assert
    assert claims["user_id"] == str(USER_ID)
    assert claims["sub"] == f"user:{USER_ID}"
    assert claims["roles"] == ["consumer"]


def test_issue_user_token_uses_a_new_uuid_jti_on_each_call(key_pair: KeyPair) -> None:
    # Arrange / Act
    first = _unverified_claims(issue_user_token(key_pair.private_pem, USER_ID, NOW))["jti"]
    second = _unverified_claims(issue_user_token(key_pair.private_pem, USER_ID, NOW))["jti"]

    # Assert
    assert first != second
    assert str(uuid.UUID(first)) == first
    assert str(uuid.UUID(second)) == second
