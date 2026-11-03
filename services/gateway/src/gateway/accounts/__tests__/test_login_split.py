"""Tests for find_login / verify_login, written from docs/specs/sq2.md section 9.2 (audit M1)."""

import dataclasses
import uuid
from collections.abc import Callable

import pytest
from argon2 import PasswordHasher
from gateway.accounts import service
from gateway.accounts.service import authenticate_user, hash_password, sign_up
from gateway.clock import FixedClock
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.interfaces import DBAPICursor, ExecutionContext
from sqlalchemy.orm import Session, sessionmaker

PASSWORD = "correct horse battery staple"
EMAIL = "alice@example.com"


def _signed_up(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL, password: str = PASSWORD
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, password)
        session.commit()
    return result.user_id


@pytest.fixture
def argon2_verified_hashes(monkeypatch: pytest.MonkeyPatch) -> list[str | bytes]:
    """The hash argument of every PasswordHasher.verify call."""
    hashes: list[str | bytes] = []
    original = PasswordHasher.verify

    def recording(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        hashes.append(hash)
        return original(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", recording)
    return hashes


# --- LoginRecord -----------------------------------------------------------------------------------------


def test_login_record_is_a_frozen_dataclass_of_user_id_and_password_hash() -> None:
    # Arrange
    record = service.LoginRecord(user_id=uuid.UUID(int=7), password_hash="$argon2id$stub")

    # Act
    field_names = [f.name for f in dataclasses.fields(service.LoginRecord)]

    # Assert
    assert field_names == ["user_id", "password_hash"]
    assert record == service.LoginRecord(user_id=uuid.UUID(int=7), password_hash="$argon2id$stub")
    with pytest.raises(dataclasses.FrozenInstanceError) as exc:
        record.password_hash = "other"  # type: ignore[misc]
    assert exc.type is dataclasses.FrozenInstanceError


# --- find_login ------------------------------------------------------------------------------------------


def test_find_login_returns_the_user_id_and_the_stored_argon2id_hash(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        record = service.find_login(session, EMAIL)

    # Assert
    assert record is not None
    assert record.user_id == user_id
    assert record.password_hash.startswith("$argon2id$")
    assert PasswordHasher().verify(record.password_hash, PASSWORD) is True


def test_find_login_returns_none_for_an_unknown_email(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        record = service.find_login(session, "nobody@example.com")

    # Assert
    assert record is None


@pytest.mark.parametrize(
    "typed", ["ALICE@EXAMPLE.COM", "Alice@Example.com", "  alice@example.com  ", "\talice@example.com\n"]
)
def test_find_login_normalizes_the_email_like_sign_up(
    session_factory: sessionmaker[Session], clock: FixedClock, typed: str
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock, email="  Alice@Example.com ")

    # Act
    with session_factory() as session:
        record = service.find_login(session, typed)

    # Assert
    assert record is not None
    assert record.user_id == user_id


def test_find_login_picks_the_record_of_the_matching_email_among_several(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock, email="alice@example.com")
    bob = _signed_up(session_factory, clock, email="bob@example.com", password="bobs own passphrase")

    # Act
    with session_factory() as session:
        record = service.find_login(session, "bob@example.com")

    # Assert
    assert record is not None
    assert record.user_id == bob
    assert PasswordHasher().verify(record.password_hash, "bobs own passphrase") is True


def test_find_login_compares_the_email_directly_without_lower_on_the_column(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _signed_up(session_factory, clock)
    statements: list[str] = []

    def capture(
        conn: Connection,
        cursor: DBAPICursor,
        statement: str,
        parameters: object,
        context: ExecutionContext | None,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    bind = session_factory.kw["bind"]
    assert isinstance(bind, Engine)
    event.listen(bind, "before_cursor_execute", capture)
    try:
        # Act
        with session_factory() as session:
            service.find_login(session, EMAIL)
    finally:
        event.remove(bind, "before_cursor_execute", capture)

    # Assert
    selects = [s for s in statements if "gateway.users" in s]
    assert len(selects) == 1
    assert "lower(" not in selects[0].lower()


# --- verify_login ----------------------------------------------------------------------------------------


def test_verify_login_returns_the_user_id_for_the_right_password() -> None:
    # Arrange
    user_id = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c77")
    record = service.LoginRecord(user_id=user_id, password_hash=hash_password(PASSWORD))

    # Act
    result = service.verify_login(record, PASSWORD)

    # Assert
    assert result == user_id


def test_verify_login_returns_none_for_a_wrong_password() -> None:
    # Arrange
    record = service.LoginRecord(user_id=uuid.uuid4(), password_hash=hash_password(PASSWORD))

    # Act
    result = service.verify_login(record, "not the password")

    # Assert
    assert result is None


def test_verify_login_returns_none_for_a_password_that_only_prefixes_the_real_one() -> None:
    # Arrange
    record = service.LoginRecord(user_id=uuid.uuid4(), password_hash=hash_password(PASSWORD))

    # Act
    result = service.verify_login(record, PASSWORD[:-1])

    # Assert
    assert result is None


def test_verify_login_needs_no_session_once_the_record_is_read(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)
    with session_factory() as session:
        record = service.find_login(session, EMAIL)

    # Act
    result = service.verify_login(record, PASSWORD)

    # Assert
    assert result == user_id


def test_verify_login_with_no_record_returns_none_after_one_argon2_verification_against_a_dummy_hash(
    session_factory: sessionmaker[Session], clock: FixedClock, argon2_verified_hashes: list[str | bytes]
) -> None:
    # Arrange
    _signed_up(session_factory, clock)
    with session_factory() as session:
        real_hash = service.find_login(session, EMAIL)
    assert real_hash is not None
    argon2_verified_hashes.clear()

    # Act
    result = service.verify_login(None, PASSWORD)

    # Assert
    assert result is None
    assert len(argon2_verified_hashes) == 1
    dummy = argon2_verified_hashes[0]
    assert isinstance(dummy, str)
    assert dummy.startswith("$argon2id$")
    assert dummy != real_hash.password_hash


def test_verify_login_with_a_record_runs_one_argon2_verification_against_that_records_hash(
    argon2_verified_hashes: list[str | bytes],
) -> None:
    # Arrange
    record = service.LoginRecord(user_id=uuid.uuid4(), password_hash=hash_password(PASSWORD))
    argon2_verified_hashes.clear()

    # Act
    service.verify_login(record, PASSWORD)

    # Assert
    assert argon2_verified_hashes == [record.password_hash]


def test_verify_login_with_no_record_uses_the_same_dummy_hash_every_time(
    argon2_verified_hashes: list[str | bytes],
) -> None:
    # Arrange
    argon2_verified_hashes.clear()

    # Act
    service.verify_login(None, PASSWORD)
    service.verify_login(None, "another password")

    # Assert
    assert len(argon2_verified_hashes) == 2
    assert argon2_verified_hashes[0] == argon2_verified_hashes[1]


# --- authenticate_user = verify_login(find_login(...)) ---------------------------------------------------


def test_authenticate_user_returns_what_verify_login_returns_for_what_find_login_found(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    found = service.LoginRecord(user_id=uuid.UUID(int=1), password_hash="$argon2id$stub")
    answer = uuid.UUID(int=2)
    seen: list[tuple[str, object]] = []

    def fake_find_login(session: Session, email: str) -> service.LoginRecord | None:
        seen.append(("find_login", email))
        return found

    def fake_verify_login(record: service.LoginRecord | None, password: str) -> uuid.UUID | None:
        seen.append(("verify_login", (record, password)))
        return answer

    monkeypatch.setattr(service, "find_login", fake_find_login)
    monkeypatch.setattr(service, "verify_login", fake_verify_login)

    # Act
    with session_factory() as session:
        result = authenticate_user(session, "typed@example.com", "typed password")

    # Assert
    assert result == answer
    assert seen == [
        ("find_login", "typed@example.com"),
        ("verify_login", (found, "typed password")),
    ]


def test_authenticate_user_keeps_its_behaviour_for_right_wrong_and_unknown(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)
    attempts: list[Callable[[Session], uuid.UUID | None]] = [
        lambda s: authenticate_user(s, "ALICE@example.com", PASSWORD),
        lambda s: authenticate_user(s, EMAIL, "wrong"),
        lambda s: authenticate_user(s, "nobody@example.com", PASSWORD),
    ]

    # Act
    with session_factory() as session:
        results = [attempt(session) for attempt in attempts]

    # Assert
    assert results == [user_id, None, None]
