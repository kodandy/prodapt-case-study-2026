"""Tests for gateway/accounts/mfa.py, written from docs/specs/gateway-basic.md section B1."""

import json
import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pyotp
import pytest
from gateway.accounts.mfa import (
    TOTP_LOCKOUT,
    TOTP_MAX_FAILURES,
    InvalidTotpCodeError,
    TotpAlreadyEnabledError,
    TotpDecryptError,
    TotpEnrolment,
    TotpLockedError,
    TotpNotEnrolledError,
    check_totp,
    confirm_totp,
    decrypt_totp_secret,
    encrypt_totp_secret,
    enrol_totp,
    totp_enabled,
)
from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

KEY = "test-totp-encryption-key-0123456789abcdef"
OTHER_KEY = "another-totp-encryption-key-fedcba9876543210"
PASSWORD = "correct horse battery staple"
EMAIL = "alice@example.com"
USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c77")
OTHER_USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c78")
FIXED_SECRET = "JBSWY3DPEHPK3PXP"
STEP_SECONDS = 30
MFA_ACTIONS = ("mfa.totp_enrolled", "mfa.totp_enabled", "mfa.totp_locked")


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _signed_up(session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _code(secret: str, clock: FixedClock, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()) + offset_steps * STEP_SECONDS)


def _wrong_code(secret: str, clock: FixedClock) -> str:
    nearby = {_code(secret, clock, offset) for offset in range(-3, 4)}
    return next(candidate for candidate in (f"{n:06d}" for n in range(1, 1000)) if candidate not in nearby)


def _outcome(call: Callable[[], object]) -> type[Exception] | None:
    try:
        call()
    except Exception as exc:  # the outcome under test is the exception type itself
        return type(exc)
    return None


def _confirm(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, code: str
) -> type[Exception] | None:
    return _outcome(lambda: confirm_totp(session_factory, clock, KEY, user_id, code))


def _check(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, code: str
) -> type[Exception] | None:
    return _outcome(lambda: check_totp(session_factory, clock, KEY, user_id, code))


def _enrolled(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL
) -> tuple[uuid.UUID, TotpEnrolment]:
    user_id = _signed_up(session_factory, clock, email)
    return user_id, enrol_totp(session_factory, clock, KEY, user_id)


def _enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL
) -> tuple[uuid.UUID, str]:
    """A user with TOTP enabled; the clock is then moved an hour on so every window step is unused."""
    user_id, enrolment = _enrolled(session_factory, clock, email)
    confirm_totp(session_factory, clock, KEY, user_id, _code(enrolment.secret, clock))
    clock.advance(timedelta(hours=1))
    return user_id, enrolment.secret


def _locked(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL
) -> tuple[uuid.UUID, str]:
    user_id, secret = _enabled(session_factory, clock, email)
    for _ in range(TOTP_MAX_FAILURES):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))
    return user_id, secret


def _stored_ciphertext(session_factory: sessionmaker[Session]) -> bytes:
    rows = _rows(session_factory, "SELECT secret_ciphertext FROM gateway.user_totp")
    assert len(rows) == 1
    value = rows[0]["secret_ciphertext"]
    assert isinstance(value, bytes | memoryview)
    return bytes(value)


def _mfa_audit(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, summary FROM gateway.audit_log "
        "WHERE action LIKE 'mfa.%' ORDER BY id",
    )


# --- constants -------------------------------------------------------------------------------------------


def test_lockout_constants_match_the_spec() -> None:
    # Arrange / Act
    constants = (TOTP_MAX_FAILURES, TOTP_LOCKOUT)

    # Assert
    assert constants == (5, timedelta(minutes=15))


# --- encrypt_totp_secret / decrypt_totp_secret -----------------------------------------------------------


def test_decrypt_returns_the_secret_that_encrypt_sealed() -> None:
    # Arrange
    blob = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)

    # Act
    secret = decrypt_totp_secret(KEY, USER_ID, blob)

    # Assert
    assert secret == FIXED_SECRET


def test_encrypted_blob_never_contains_the_plaintext_secret() -> None:
    # Arrange / Act
    blob = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)

    # Assert
    assert FIXED_SECRET.encode() not in blob
    assert FIXED_SECRET.lower().encode() not in blob


def test_encrypted_blob_is_a_12_byte_nonce_then_ciphertext_and_a_16_byte_gcm_tag() -> None:
    # Arrange / Act
    blob = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)

    # Assert
    assert len(blob) == 12 + len(FIXED_SECRET) + 16


def test_encrypting_the_same_secret_twice_gives_different_blobs() -> None:
    # Arrange / Act
    first = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)
    second = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)

    # Assert
    assert first != second
    assert first[:12] != second[:12]


def test_decrypt_with_a_different_key_raises_totp_decrypt_error() -> None:
    # Arrange
    blob = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)

    # Act
    with pytest.raises(TotpDecryptError) as exc:
        decrypt_totp_secret(OTHER_KEY, USER_ID, blob)

    # Assert
    assert exc.type is TotpDecryptError


def test_decrypt_with_another_users_id_raises_totp_decrypt_error() -> None:
    # Arrange
    blob = encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET)

    # Act
    with pytest.raises(TotpDecryptError) as exc:
        decrypt_totp_secret(KEY, OTHER_USER_ID, blob)

    # Assert
    assert exc.type is TotpDecryptError


def test_decrypt_of_a_tampered_blob_raises_totp_decrypt_error() -> None:
    # Arrange
    blob = bytearray(encrypt_totp_secret(KEY, USER_ID, FIXED_SECRET))
    blob[-1] ^= 0x01

    # Act
    with pytest.raises(TotpDecryptError) as exc:
        decrypt_totp_secret(KEY, USER_ID, bytes(blob))

    # Assert
    assert exc.type is TotpDecryptError


# --- enrol_totp ------------------------------------------------------------------------------------------


def test_enrol_returns_a_base32_secret_and_an_otpauth_uri_for_that_secret(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    enrolment = enrol_totp(session_factory, clock, KEY, user_id)

    # Assert
    parsed = pyotp.parse_uri(enrolment.otpauth_uri)
    assert re.fullmatch(r"[A-Z2-7]{32}", enrolment.secret) is not None
    assert enrolment.otpauth_uri.startswith("otpauth://totp/")
    assert isinstance(parsed, pyotp.TOTP)
    assert (parsed.secret, parsed.issuer, parsed.digits, parsed.interval) == (
        enrolment.secret,
        "PayPlat",
        6,
        30,
    )


def test_enrol_stores_a_pending_row_whose_ciphertext_decrypts_to_the_secret(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    enrolment = enrol_totp(session_factory, clock, KEY, user_id)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT user_id, confirmed_at, last_used_step, failed_attempts, locked_until FROM gateway.user_totp",
    )
    ciphertext = _stored_ciphertext(session_factory)
    assert len(rows) == 1
    assert (rows[0]["user_id"], rows[0]["confirmed_at"], rows[0]["last_used_step"]) == (user_id, None, None)
    assert (rows[0]["failed_attempts"], rows[0]["locked_until"]) == (0, None)
    assert enrolment.secret.encode() not in ciphertext
    assert decrypt_totp_secret(KEY, user_id, ciphertext) == enrolment.secret


def test_enrol_does_not_enable_totp_until_it_is_confirmed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    enrol_totp(session_factory, clock, KEY, user_id)

    # Assert
    with session_factory() as session:
        assert totp_enabled(session, user_id) is False


def test_re_enrolling_before_confirmation_replaces_the_pending_secret(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, first = _enrolled(session_factory, clock)

    # Act
    second = enrol_totp(session_factory, clock, KEY, user_id)

    # Assert
    assert second.secret != first.secret
    assert decrypt_totp_secret(KEY, user_id, _stored_ciphertext(session_factory)) == second.secret


def test_confirm_after_re_enrolling_rejects_a_code_of_the_replaced_secret(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, first = _enrolled(session_factory, clock)
    second = enrol_totp(session_factory, clock, KEY, user_id)

    # Act
    old_code_outcome = _confirm(session_factory, clock, user_id, _code(first.secret, clock))
    new_code_outcome = _confirm(session_factory, clock, user_id, _code(second.secret, clock))

    # Assert
    assert (old_code_outcome, new_code_outcome) == (InvalidTotpCodeError, None)


def test_re_enrolling_resets_the_failure_counter_and_the_lock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)
    for _ in range(TOTP_MAX_FAILURES):
        _confirm(session_factory, clock, user_id, _wrong_code(enrolment.secret, clock))
    assert _confirm(session_factory, clock, user_id, _code(enrolment.secret, clock)) is TotpLockedError

    # Act
    renewed = enrol_totp(session_factory, clock, KEY, user_id)
    outcome = _confirm(session_factory, clock, user_id, _code(renewed.secret, clock))

    # Assert
    assert outcome is None
    with session_factory() as session:
        assert totp_enabled(session, user_id) is True


def test_enrolling_when_totp_is_already_confirmed_raises_totp_already_enabled_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    outcome = _outcome(lambda: enrol_totp(session_factory, clock, KEY, user_id))

    # Assert
    assert outcome is TotpAlreadyEnabledError
    assert decrypt_totp_secret(KEY, user_id, _stored_ciphertext(session_factory)) == secret


# --- totp_enabled ----------------------------------------------------------------------------------------


def test_totp_enabled_is_false_for_a_user_who_never_enrolled(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    with session_factory() as session:
        enabled = totp_enabled(session, user_id)

    # Assert
    assert enabled is False


def test_totp_enabled_is_true_once_confirmed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, _ = _enabled(session_factory, clock)

    # Act
    with session_factory() as session:
        enabled = totp_enabled(session, user_id)

    # Assert
    assert enabled is True


def test_totp_enabled_is_per_user(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    _enabled(session_factory, clock, "alice@example.com")
    bob = _signed_up(session_factory, clock, "bob@example.com")

    # Act
    with session_factory() as session:
        enabled = totp_enabled(session, bob)

    # Assert
    assert enabled is False


# --- confirm_totp ----------------------------------------------------------------------------------------


def test_confirm_with_the_current_code_sets_confirmed_at_and_records_the_step(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)

    # Act
    confirm_totp(session_factory, clock, KEY, user_id, _code(enrolment.secret, clock))

    # Assert
    rows = _rows(
        session_factory, "SELECT confirmed_at, last_used_step, failed_attempts FROM gateway.user_totp"
    )
    assert rows == [
        {
            "confirmed_at": clock(),
            "last_used_step": int(clock().timestamp()) // STEP_SECONDS,
            "failed_attempts": 0,
        }
    ]


def test_confirm_without_an_enrolment_raises_totp_not_enrolled_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    outcome = _confirm(session_factory, clock, user_id, "123456")

    # Assert
    assert outcome is TotpNotEnrolledError


def test_confirm_when_already_confirmed_raises_totp_already_enabled_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    outcome = _confirm(session_factory, clock, user_id, _code(secret, clock))

    # Assert
    assert outcome is TotpAlreadyEnabledError


def test_confirm_with_a_wrong_code_raises_invalid_totp_code_error_and_leaves_totp_disabled(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)

    # Act
    outcome = _confirm(session_factory, clock, user_id, _wrong_code(enrolment.secret, clock))

    # Assert
    assert outcome is InvalidTotpCodeError
    with session_factory() as session:
        assert totp_enabled(session, user_id) is False


def test_confirm_while_locked_raises_totp_locked_error_even_for_a_correct_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)
    for _ in range(TOTP_MAX_FAILURES):
        _confirm(session_factory, clock, user_id, _wrong_code(enrolment.secret, clock))

    # Act
    outcome = _confirm(session_factory, clock, user_id, _code(enrolment.secret, clock))

    # Assert
    assert outcome is TotpLockedError
    with session_factory() as session:
        assert totp_enabled(session, user_id) is False


# --- check_totp: window and replay -----------------------------------------------------------------------


@pytest.mark.parametrize("offset_steps", [-1, 0, 1])
def test_check_accepts_codes_within_one_step_of_now(
    session_factory: sessionmaker[Session], clock: FixedClock, offset_steps: int
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock, offset_steps))

    # Assert
    assert outcome is None


@pytest.mark.parametrize("offset_steps", [-2, 2])
def test_check_rejects_codes_two_steps_away(
    session_factory: sessionmaker[Session], clock: FixedClock, offset_steps: int
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock, offset_steps))

    # Assert
    assert outcome is InvalidTotpCodeError


def test_check_rejects_a_replay_of_the_same_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    code = _code(secret, clock)
    first = _check(session_factory, clock, user_id, code)

    # Act
    second = _check(session_factory, clock, user_id, code)

    # Assert
    assert (first, second) == (None, InvalidTotpCodeError)


def test_check_rejects_a_code_of_an_older_step_than_the_last_accepted_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    newer = _check(session_factory, clock, user_id, _code(secret, clock, 1))

    # Act
    older = _check(session_factory, clock, user_id, _code(secret, clock, 0))

    # Assert
    assert (newer, older) == (None, InvalidTotpCodeError)


def test_check_accepts_the_next_step_after_a_used_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    first = _check(session_factory, clock, user_id, _code(secret, clock))

    # Act
    second = _check(session_factory, clock, user_id, _code(secret, clock, 1))

    # Assert
    assert (first, second) == (None, None)


def test_check_with_a_code_of_another_users_secret_raises_invalid_totp_code_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _enabled(session_factory, clock, "alice@example.com")
    _, bob_secret = _enabled(session_factory, clock, "bob@example.com")

    # Act
    outcome = _check(session_factory, clock, alice, _code(bob_secret, clock))

    # Assert
    assert outcome is InvalidTotpCodeError


def test_two_concurrent_checks_of_one_code_cannot_both_pass(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    def race(code: str) -> list[type[Exception] | None]:
        barrier = Barrier(2)

        def attempt() -> type[Exception] | None:
            barrier.wait()
            return _check(session_factory, clock, user_id, code)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(attempt), pool.submit(attempt)]
            return [future.result() for future in futures]

    # Act
    rounds: list[list[type[Exception] | None]] = []
    for _ in range(5):
        clock.advance(timedelta(seconds=2 * STEP_SECONDS))
        rounds.append(race(_code(secret, clock)))

    # Assert
    assert [sum(outcome is None for outcome in outcomes) for outcomes in rounds] == [1] * 5
    assert [[o for o in outcomes if o is not None] for outcomes in rounds] == [[InvalidTotpCodeError]] * 5


# --- check_totp: malformed codes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_code",
    ["", "12345", "1234567", "abcdef", " 12345", "12 456", "١٢٣٤٥٦", "12345\n", "+12345", "1.2345"],
)
def test_check_rejects_a_code_that_is_not_exactly_six_ascii_digits(
    session_factory: sessionmaker[Session], clock: FixedClock, bad_code: str
) -> None:
    # Arrange
    user_id, _ = _enabled(session_factory, clock)

    # Act
    outcome = _check(session_factory, clock, user_id, bad_code)

    # Assert
    assert outcome is InvalidTotpCodeError


def test_a_correct_code_with_a_trailing_newline_is_rejected(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock) + "\n")

    # Assert
    assert outcome is InvalidTotpCodeError


def test_malformed_codes_count_towards_the_lockout(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    for bad_code in ("", "12345", "1234567", "abcdef", "12 456"):
        _check(session_factory, clock, user_id, bad_code)

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock))

    # Assert
    assert outcome is TotpLockedError


# --- check_totp: lockout ---------------------------------------------------------------------------------


def test_four_wrong_codes_do_not_lock_and_a_correct_code_still_passes(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    wrong = [_check(session_factory, clock, user_id, _wrong_code(secret, clock)) for _ in range(4)]

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock))

    # Assert
    assert wrong == [InvalidTotpCodeError] * 4
    assert outcome is None


def test_the_failure_counter_survives_the_raise_so_the_sixth_call_with_a_correct_code_is_locked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    for _ in range(TOTP_MAX_FAILURES):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))

    # Act
    with pytest.raises(TotpLockedError) as exc:
        check_totp(session_factory, clock, KEY, user_id, _code(secret, clock))

    # Assert
    assert exc.type is TotpLockedError
    rows = _rows(session_factory, "SELECT failed_attempts, locked_until FROM gateway.user_totp")
    assert rows == [{"failed_attempts": 5, "locked_until": clock() + TOTP_LOCKOUT}]


def test_failed_attempts_are_persisted_per_call(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    for _ in range(3):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))

    # Assert
    assert _rows(session_factory, "SELECT failed_attempts FROM gateway.user_totp") == [{"failed_attempts": 3}]


def test_locked_check_rejects_a_correct_code_until_the_lockout_has_passed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _locked(session_factory, clock)
    clock.advance(TOTP_LOCKOUT - timedelta(seconds=1))

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock))

    # Assert
    assert outcome is TotpLockedError


def test_check_works_again_after_the_fifteen_minute_lockout(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _locked(session_factory, clock)
    clock.advance(TOTP_LOCKOUT + timedelta(seconds=1))

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock))

    # Assert
    assert outcome is None


def test_a_wrong_code_during_the_lock_is_still_reported_as_locked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _locked(session_factory, clock)

    # Act
    outcome = _check(session_factory, clock, user_id, _wrong_code(secret, clock))

    # Assert
    assert outcome is TotpLockedError


def test_a_success_resets_the_failure_counter(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)
    for _ in range(4):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))
    success = _check(session_factory, clock, user_id, _code(secret, clock))
    for _ in range(4):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))

    # Act
    outcome = _check(session_factory, clock, user_id, _code(secret, clock, 1))

    # Assert
    assert (success, outcome) == (None, None)
    assert _rows(session_factory, "SELECT failed_attempts FROM gateway.user_totp") == [{"failed_attempts": 0}]


def test_one_users_lockout_does_not_lock_another_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _locked(session_factory, clock, "alice@example.com")
    bob, bob_secret = _enabled(session_factory, clock, "bob@example.com")

    # Act
    outcome = _check(session_factory, clock, bob, _code(bob_secret, clock))

    # Assert
    assert outcome is None


# --- audit -----------------------------------------------------------------------------------------------


def test_enrolling_writes_one_mfa_totp_enrolled_row_for_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id = _signed_up(session_factory, clock)

    # Act
    enrol_totp(session_factory, clock, KEY, user_id)

    # Assert
    rows = _mfa_audit(session_factory)
    assert [(r["actor_id"], r["actor_role"], r["action"]) for r in rows] == [
        (f"user:{user_id}", "consumer", "mfa.totp_enrolled")
    ]


def test_confirming_writes_an_mfa_totp_enabled_row_for_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)

    # Act
    confirm_totp(session_factory, clock, KEY, user_id, _code(enrolment.secret, clock))

    # Assert
    rows = _mfa_audit(session_factory)
    assert [(r["actor_id"], r["actor_role"], r["action"]) for r in rows] == [
        (f"user:{user_id}", "consumer", "mfa.totp_enrolled"),
        (f"user:{user_id}", "consumer", "mfa.totp_enabled"),
    ]


def test_a_failed_confirmation_writes_no_mfa_totp_enabled_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)

    # Act
    _confirm(session_factory, clock, user_id, _wrong_code(enrolment.secret, clock))

    # Assert
    assert [r["action"] for r in _mfa_audit(session_factory)] == ["mfa.totp_enrolled"]


def test_reaching_the_lockout_writes_exactly_one_mfa_totp_locked_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    for _ in range(TOTP_MAX_FAILURES):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))
    _check(session_factory, clock, user_id, _code(secret, clock))

    # Assert
    locked = [r for r in _mfa_audit(session_factory) if r["action"] == "mfa.totp_locked"]
    assert [(r["actor_id"], r["actor_role"]) for r in locked] == [(f"user:{user_id}", "consumer")]


def test_four_failures_write_no_mfa_totp_locked_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, secret = _enabled(session_factory, clock)

    # Act
    for _ in range(4):
        _check(session_factory, clock, user_id, _wrong_code(secret, clock))

    # Assert
    assert [r["action"] for r in _mfa_audit(session_factory)] == ["mfa.totp_enrolled", "mfa.totp_enabled"]


def test_audit_summaries_never_contain_the_secret_or_any_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    user_id, enrolment = _enrolled(session_factory, clock)
    used_codes = [_code(enrolment.secret, clock, offset) for offset in (-1, 0, 1)]
    wrong_codes = [_wrong_code(enrolment.secret, clock)]
    confirm_totp(session_factory, clock, KEY, user_id, used_codes[1])
    clock.advance(timedelta(hours=1))
    for _ in range(TOTP_MAX_FAILURES):
        wrong = _wrong_code(enrolment.secret, clock)
        wrong_codes.append(wrong)
        _check(session_factory, clock, user_id, wrong)

    # Act
    rows = _mfa_audit(session_factory)

    # Assert
    serialized = json.dumps(rows, default=str)
    assert [r["action"] for r in rows] == ["mfa.totp_enrolled", "mfa.totp_enabled", "mfa.totp_locked"]
    for forbidden in (enrolment.secret, enrolment.otpauth_uri, KEY, *used_codes, *wrong_codes):
        assert forbidden not in serialized
