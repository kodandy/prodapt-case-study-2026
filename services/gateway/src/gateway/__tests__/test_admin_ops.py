"""Tests for the ops principals in gateway/admin.py and issue_ops_token, from docs/specs/compliance-rbac.md
section 2 (ops principals) and section 3 (ops-ai token)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from gateway.accounts.sessions import issue_session_token
from gateway.accounts.tokens import issue_merchant_token, issue_ops_token
from gateway.admin import (
    OPS_LOCKOUT,
    OPS_MAX_FAILURES,
    OPS_SESSION_AUDIENCE,
    OPS_SESSION_TTL_SECONDS,
    InvalidOpsCredentialsError,
    InvalidOpsSessionError,
    OpsEnrolment,
    OpsLockedError,
    OpsPrincipal,
    OpsRole,
    authenticate_ops,
    bootstrap_ops_user,
    issue_ops_session_token,
    verify_ops_session_token,
)
from gateway.clock import FixedClock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
EMAIL = "agent@ops.example.com"
PASSWORD = "ops-correct-horse-battery-9Z"
WRONG_PASSWORD = "ops-wrong-horse-battery-1A"
STEP_SECONDS = 30
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
OPS_USER_ID = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c91")


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


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _audit(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log ORDER BY id",
    )


def _enrol(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    role: OpsRole = "ops_agent",
    email: str = EMAIL,
) -> OpsEnrolment:
    return bootstrap_ops_user(session_factory, clock, email, PASSWORD, role, TOTP_KEY)


def _code(secret: str, clock: FixedClock, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()) + offset_steps * STEP_SECONDS)


def _wrong_code(secret: str, clock: FixedClock) -> str:
    nearby = {_code(secret, clock, offset) for offset in range(-3, 4)}
    return next(candidate for candidate in (f"{n:06d}" for n in range(1, 1000)) if candidate not in nearby)


def _login(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    code: str,
    email: str = EMAIL,
    password: str = PASSWORD,
) -> OpsPrincipal:
    return authenticate_ops(session_factory, clock, TOTP_KEY, email, password, code)


def _unverified(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def _signed(private_pem: str, **overrides: object) -> str:
    issued_at = int(NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": OPS_SESSION_AUDIENCE,
        "sub": f"ops:{OPS_USER_ID}",
        "ops_user_id": str(OPS_USER_ID),
        "role": "ops_lead",
        "amr": ["pwd", "otp"],
        "iat": issued_at,
        "exp": issued_at + OPS_SESSION_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_pem, algorithm="RS256")


# --- constants ---------------------------------------------------------------------------------------------


def test_ops_constants_match_the_spec() -> None:
    # Arrange / Act
    constants = (OPS_SESSION_AUDIENCE, OPS_SESSION_TTL_SECONDS, OPS_MAX_FAILURES, OPS_LOCKOUT)

    # Assert
    assert constants == ("gateway-ops", 900, 5, timedelta(minutes=15))


# --- bootstrap_ops_user ------------------------------------------------------------------------------------


def test_bootstrap_ops_user_returns_an_enrolment_whose_secret_generates_accepted_codes(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    principal = _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert principal == OpsPrincipal(ops_user_id=enrolment.ops_user_id, role="ops_agent")


def test_bootstrap_ops_user_totp_secret_is_a_valid_base32_secret_that_pyotp_accepts(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    code = pyotp.TOTP(enrolment.totp_secret).at(int(clock().timestamp()))

    # Assert
    assert len(code) == 6
    assert code.isdigit()


def test_bootstrap_ops_user_stores_the_given_id_and_role(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    enrolment = bootstrap_ops_user(
        session_factory, clock, EMAIL, PASSWORD, "ops_lead", TOTP_KEY, ops_user_id=OPS_USER_ID
    )

    # Assert
    rows = _rows(session_factory, "SELECT id, role, failed_attempts, disabled_at FROM gateway.ops_users")
    assert enrolment.ops_user_id == OPS_USER_ID
    assert rows == [{"id": OPS_USER_ID, "role": "ops_lead", "failed_attempts": 0, "disabled_at": None}]


def test_bootstrap_ops_user_enrolment_repr_does_not_leak_the_totp_secret(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    rendered = repr(enrolment)

    # Assert
    assert enrolment.totp_secret not in rendered


def test_bootstrap_ops_user_stores_an_argon2id_password_hash_and_never_the_password(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _enrol(session_factory, clock)

    # Act
    rows = _rows(session_factory, "SELECT password_hash FROM gateway.ops_users")

    # Assert
    stored = str(rows[0]["password_hash"])
    assert stored.startswith("$argon2id$")
    assert PASSWORD not in stored


def test_bootstrap_ops_user_stores_the_totp_secret_only_as_ciphertext(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    rows = _rows(session_factory, "SELECT totp_secret_ciphertext FROM gateway.ops_users")

    # Assert
    raw = rows[0]["totp_secret_ciphertext"]
    assert isinstance(raw, bytes | memoryview)
    ciphertext = bytes(raw)
    assert len(ciphertext) > 0
    assert enrolment.totp_secret.encode() not in ciphertext


def test_bootstrap_ops_user_records_creation_time_from_the_clock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _enrol(session_factory, clock)

    # Act
    rows = _rows(session_factory, "SELECT created_at, totp_last_used_step FROM gateway.ops_users")

    # Assert
    assert rows == [{"created_at": clock(), "totp_last_used_step": None}]


# --- authenticate_ops: success -----------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_authenticate_ops_returns_the_principal_with_the_stored_role(
    session_factory: sessionmaker[Session], clock: FixedClock, role: OpsRole
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock, role=role)

    # Act
    principal = _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert principal == OpsPrincipal(ops_user_id=enrolment.ops_user_id, role=role)


def test_authenticate_ops_success_records_the_used_totp_step_and_a_login_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    steps = _rows(session_factory, "SELECT totp_last_used_step FROM gateway.ops_users")
    audit = _audit(session_factory)
    assert steps == [{"totp_last_used_step": int(clock().timestamp()) // STEP_SECONDS}]
    assert [(row["actor_id"], row["actor_role"], row["action"]) for row in audit] == [
        (f"ops:{enrolment.ops_user_id}", "ops_agent", "ops.login")
    ]


def test_authenticate_ops_audit_rows_never_carry_the_password_or_the_totp_secret(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    code = _code(enrolment.totp_secret, clock)

    # Act
    _login(session_factory, clock, code)
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, code, password=WRONG_PASSWORD)

    # Assert
    rendered = repr(_audit(session_factory))
    assert PASSWORD not in rendered
    assert WRONG_PASSWORD not in rendered
    assert enrolment.totp_secret not in rendered


# --- authenticate_ops: invalid credentials ---------------------------------------------------------------


def test_authenticate_ops_rejects_an_unknown_email(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act / Assert
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, _code(enrolment.totp_secret, clock), email="nobody@ops.example.com")


def test_authenticate_ops_rejects_a_wrong_password(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act / Assert
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, _code(enrolment.totp_secret, clock), password=WRONG_PASSWORD)


def test_authenticate_ops_rejects_a_wrong_totp_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act / Assert
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, _wrong_code(enrolment.totp_secret, clock))


@pytest.mark.parametrize("code", ["", "abcdef", "12345", "1234567"])
def test_authenticate_ops_rejects_a_malformed_totp_code(
    session_factory: sessionmaker[Session], clock: FixedClock, code: str
) -> None:
    # Arrange
    _enrol(session_factory, clock)

    # Act / Assert
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, code)


def test_authenticate_ops_rejects_a_replayed_code_from_the_same_step(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    code = _code(enrolment.totp_secret, clock)
    _login(session_factory, clock, code)

    # Act / Assert
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, code)


def test_authenticate_ops_accepts_a_fresh_code_from_a_later_step(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _login(session_factory, clock, _code(enrolment.totp_secret, clock))
    clock.advance(timedelta(seconds=2 * STEP_SECONDS))

    # Act
    principal = _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert principal == OpsPrincipal(ops_user_id=enrolment.ops_user_id, role="ops_agent")


def test_authenticate_ops_a_failure_writes_a_login_failed_audit_row_for_the_ops_actor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, _code(enrolment.totp_secret, clock), password=WRONG_PASSWORD)

    # Assert
    audit = _audit(session_factory)
    assert [(row["actor_id"], row["action"]) for row in audit] == [
        (f"ops:{enrolment.ops_user_id}", "ops.login_failed")
    ]


# --- authenticate_ops: lockout -----------------------------------------------------------------------------


def _fail_times(
    session_factory: sessionmaker[Session], clock: FixedClock, secret: str, count: int
) -> list[type[Exception]]:
    outcomes: list[type[Exception]] = []
    for _ in range(count):
        try:
            _login(session_factory, clock, _code(secret, clock), password=WRONG_PASSWORD)
        except (InvalidOpsCredentialsError, OpsLockedError) as exc:
            outcomes.append(type(exc))
    return outcomes


def test_four_consecutive_failures_do_not_lock_the_account(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)

    # Act
    outcomes = _fail_times(session_factory, clock, enrolment.totp_secret, 4)
    principal = _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert outcomes == [InvalidOpsCredentialsError] * 4
    assert principal == OpsPrincipal(ops_user_id=enrolment.ops_user_id, role="ops_agent")


def test_the_fifth_consecutive_failure_locks_for_fifteen_minutes(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _fail_times(session_factory, clock, enrolment.totp_secret, 4)

    # Act
    with pytest.raises(OpsLockedError) as locked:
        _login(session_factory, clock, _code(enrolment.totp_secret, clock), password=WRONG_PASSWORD)

    # Assert
    assert locked.value.retry_after == 900


def test_failures_of_every_kind_count_towards_the_same_lockout(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    secret = enrolment.totp_secret
    _fail_times(session_factory, clock, secret, 3)
    with pytest.raises(InvalidOpsCredentialsError):
        _login(session_factory, clock, _wrong_code(secret, clock))

    # Act
    with pytest.raises(OpsLockedError) as locked:
        _login(session_factory, clock, _wrong_code(secret, clock))

    # Assert
    assert locked.value.retry_after == 900


def test_a_locked_account_is_refused_even_with_the_correct_credentials(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _fail_times(session_factory, clock, enrolment.totp_secret, 5)

    # Act
    with pytest.raises(OpsLockedError) as locked:
        _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert locked.value.retry_after == 900


def test_a_locked_account_reports_the_remaining_lock_time(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _fail_times(session_factory, clock, enrolment.totp_secret, 5)
    clock.advance(timedelta(seconds=60))

    # Act
    with pytest.raises(OpsLockedError) as locked:
        _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert locked.value.retry_after == 840


def test_the_account_is_still_locked_one_second_before_the_lock_ends(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _fail_times(session_factory, clock, enrolment.totp_secret, 5)
    clock.advance(timedelta(minutes=15) - timedelta(seconds=1))

    # Act
    with pytest.raises(OpsLockedError) as locked:
        _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert locked.value.retry_after == 1


def test_the_account_works_again_fifteen_minutes_and_a_second_after_the_lock(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _fail_times(session_factory, clock, enrolment.totp_secret, 5)
    clock.advance(timedelta(minutes=15, seconds=1))

    # Act
    principal = _login(session_factory, clock, _code(enrolment.totp_secret, clock))

    # Assert
    assert principal == OpsPrincipal(ops_user_id=enrolment.ops_user_id, role="ops_agent")


def test_a_success_resets_the_failure_counter(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    _fail_times(session_factory, clock, enrolment.totp_secret, 4)
    _login(session_factory, clock, _code(enrolment.totp_secret, clock))
    clock.advance(timedelta(seconds=2 * STEP_SECONDS))

    # Act
    outcomes = _fail_times(session_factory, clock, enrolment.totp_secret, 4)

    # Assert
    counters = _rows(session_factory, "SELECT failed_attempts, locked_until FROM gateway.ops_users")
    assert outcomes == [InvalidOpsCredentialsError] * 4
    assert counters == [{"failed_attempts": 4, "locked_until": None}]


def test_lockout_writes_login_failed_rows_then_one_login_locked_row_for_the_ops_actor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    enrolment = _enrol(session_factory, clock)
    actor = f"ops:{enrolment.ops_user_id}"

    # Act
    _fail_times(session_factory, clock, enrolment.totp_secret, 5)

    # Assert
    audit = _audit(session_factory)
    failed = [row for row in audit if row["action"] == "ops.login_failed"]
    locked = [row for row in audit if row["action"] == "ops.login_locked"]
    assert len(failed) >= 4
    assert [row["actor_id"] for row in locked] == [actor]
    assert {row["actor_id"] for row in audit} == {actor}


def test_locking_one_ops_account_does_not_lock_another(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _enrol(session_factory, clock)
    second = _enrol(session_factory, clock, role="ops_lead", email="lead@ops.example.com")
    _fail_times(session_factory, clock, first.totp_secret, 5)

    # Act
    principal = _login(session_factory, clock, _code(second.totp_secret, clock), email="lead@ops.example.com")

    # Assert
    assert principal == OpsPrincipal(ops_user_id=second.ops_user_id, role="ops_lead")


# --- ops session token -------------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_ops_session_token_round_trips_the_principal(key_pair: KeyPair, role: OpsRole) -> None:
    # Arrange
    principal = OpsPrincipal(ops_user_id=OPS_USER_ID, role=role)
    token = issue_ops_session_token(key_pair.private_pem, principal, NOW)

    # Act
    verified = verify_ops_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert verified == principal


def test_ops_session_token_carries_the_spec_claims(key_pair: KeyPair) -> None:
    # Arrange
    principal = OpsPrincipal(ops_user_id=OPS_USER_ID, role="ops_lead")

    # Act
    claims = _unverified(issue_ops_session_token(key_pair.private_pem, principal, NOW))

    # Assert
    issued_at = int(NOW.timestamp())
    assert claims["aud"] == "gateway-ops"
    assert claims["sub"] == f"ops:{OPS_USER_ID}"
    assert claims["ops_user_id"] == str(OPS_USER_ID)
    assert claims["role"] == "ops_lead"
    assert claims["amr"] == ["pwd", "otp"]
    assert claims["iat"] == issued_at
    assert claims["exp"] == issued_at + 900


def test_ops_session_token_header_algorithm_is_rs256(key_pair: KeyPair) -> None:
    # Arrange
    principal = OpsPrincipal(ops_user_id=OPS_USER_ID, role="ops_agent")

    # Act
    token = issue_ops_session_token(key_pair.private_pem, principal, NOW)

    # Assert
    assert jwt.get_unverified_header(token)["alg"] == "RS256"


def test_ops_session_token_is_valid_until_the_last_second_before_expiry(key_pair: KeyPair) -> None:
    # Arrange
    principal = OpsPrincipal(ops_user_id=OPS_USER_ID, role="ops_agent")
    token = issue_ops_session_token(key_pair.private_pem, principal, NOW)

    # Act
    verified = verify_ops_session_token(key_pair.private_pem, token, NOW + timedelta(seconds=899))

    # Assert
    assert verified == principal


def test_expired_ops_session_token_is_rejected(key_pair: KeyPair) -> None:
    # Arrange
    principal = OpsPrincipal(ops_user_id=OPS_USER_ID, role="ops_agent")
    token = issue_ops_session_token(key_pair.private_pem, principal, NOW)

    # Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW + timedelta(seconds=900))


def test_ops_session_token_signed_by_another_key_is_rejected(
    key_pair: KeyPair, other_key_pair: KeyPair
) -> None:
    # Arrange
    token = _signed(other_key_pair.private_pem)

    # Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW)


def test_a_consumer_session_token_is_not_an_ops_session(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_session_token(key_pair.private_pem, uuid.uuid4(), NOW, ("pwd", "otp"))

    # Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW)


def test_a_merchant_token_is_not_an_ops_session(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_merchant_token(key_pair.private_pem, uuid.uuid4(), NOW)

    # Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW)


def test_an_ops_ai_token_is_not_an_ops_session(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_ops_token(key_pair.private_pem, OPS_USER_ID, "ops_lead", NOW)

    # Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW)


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c"])
def test_a_malformed_ops_session_token_is_rejected(key_pair: KeyPair, token: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"role": "ops_admin"},
        {"role": "consumer"},
        {"role": 7},
        {"sub": f"ops:{uuid.uuid4()}"},
        {"sub": f"user:{OPS_USER_ID}"},
        {"ops_user_id": "not-a-uuid", "sub": "ops:not-a-uuid"},
        {"ops_user_id": 7},
    ],
    ids=[
        "unknown_role",
        "consumer_role",
        "non_string_role",
        "sub_mismatch",
        "wrong_sub_kind",
        "non_uuid_id",
        "non_string_id",
    ],
)
def test_an_ops_session_token_with_invalid_claims_is_rejected(
    key_pair: KeyPair, overrides: dict[str, object]
) -> None:
    # Arrange
    token = _signed(key_pair.private_pem, **overrides)

    # Act / Assert
    with pytest.raises(InvalidOpsSessionError):
        verify_ops_session_token(key_pair.private_pem, token, NOW)


def test_a_well_formed_hand_signed_ops_session_token_is_accepted(key_pair: KeyPair) -> None:
    # Arrange
    token = _signed(key_pair.private_pem)

    # Act
    verified = verify_ops_session_token(key_pair.private_pem, token, NOW)

    # Assert
    assert verified == OpsPrincipal(ops_user_id=OPS_USER_ID, role="ops_lead")


# --- issue_ops_token (ops-ai) ------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_issue_ops_token_carries_the_ops_ai_claims(key_pair: KeyPair, role: OpsRole) -> None:
    # Arrange
    issued_at = int(NOW.timestamp())

    # Act
    token = issue_ops_token(key_pair.private_pem, OPS_USER_ID, role, NOW)

    # Assert
    claims = _unverified(token)
    assert claims == {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"ops:{OPS_USER_ID}",
        "ops_user_id": str(OPS_USER_ID),
        "roles": [role],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": claims.get("jti"),
    }


def test_issue_ops_token_header_algorithm_is_rs256_and_jti_is_a_uuid(key_pair: KeyPair) -> None:
    # Arrange / Act
    token = issue_ops_token(key_pair.private_pem, OPS_USER_ID, "ops_agent", NOW)

    # Assert
    jti = str(_unverified(token)["jti"])
    assert jwt.get_unverified_header(token)["alg"] == "RS256"
    assert str(uuid.UUID(jti)) == jti


def test_issue_ops_token_verifies_against_the_public_key_for_the_ops_ai_audience(key_pair: KeyPair) -> None:
    # Arrange
    token = issue_ops_token(key_pair.private_pem, OPS_USER_ID, "ops_agent", NOW)

    # Act
    claims = jwt.decode(
        token,
        key_pair.public_pem,
        algorithms=["RS256"],
        audience="ops-ai",
        issuer="gateway",
        options={"verify_exp": False, "verify_iat": False},
    )

    # Assert
    assert claims["roles"] == ["ops_agent"]
