"""Tests for POST /v1/user-tokens with TOTP, written from docs/specs/gateway-basic.md (B1, Decision 6)."""

import uuid
from datetime import timedelta

import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

USER_TOKENS = "/v1/user-tokens"
SESSIONS = "/v1/sessions"
ENROL = "/v1/me/mfa/totp"
VERIFY = "/v1/me/mfa/totp/verify"
EMAIL = "decision6-user@example.com"
PASSWORD = "route-test-password-7Q"
WRONG_PASSWORD = "not-the-password-canary-81"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
STEP_SECONDS = 30
MAX_FAILURES = 5
INVALID_CREDENTIALS = {"detail": "invalid credentials"}
MFA_REQUIRED = {"detail": "mfa required"}
MFA_LOCKED = {"detail": "mfa locked"}


def _generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _public_pem(private_pem: str) -> str:
    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


@pytest.fixture(scope="module")
def private_pem() -> str:
    return _generate_private_pem()


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    *,
    totp_key: str | None = TOTP_KEY,
) -> TestClient:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(
        charge=charge,
        api_key_pepper=TEST_API_KEY_PEPPER,
        jwt_private_key_pem=private_pem,
        totp_encryption_key=totp_key,
    )
    return TestClient(create_app(deps))


def _sign_up(session_factory: sessionmaker[Session], clock: FixedClock) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, EMAIL, PASSWORD)
        session.commit()
    return result.user_id


def _audit(session_factory: sessionmaker[Session], action: str) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT actor_id, actor_role, action, summary FROM gateway.audit_log "
                "WHERE action = :action ORDER BY id"
            ),
            {"action": action},
        ).mappings()
        return [dict(row) for row in rows]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_token(client: TestClient, password: str = PASSWORD, totp_code: str | None = None) -> Response:
    body = {"email": EMAIL, "password": password}
    if totp_code is not None:
        body["totp_code"] = totp_code
    response: Response = client.post(USER_TOKENS, json=body)
    return response


def _session(client: TestClient, totp_code: str | None = None) -> Response:
    body = {"email": EMAIL, "password": PASSWORD}
    if totp_code is not None:
        body["totp_code"] = totp_code
    response: Response = client.post(SESSIONS, json=body)
    return response


def _code(secret: str, clock: FixedClock, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()) + offset_steps * STEP_SECONDS)


def _wrong_code(secret: str, clock: FixedClock) -> str:
    nearby = {_code(secret, clock, offset) for offset in range(-3, 4)}
    return next(candidate for candidate in (f"{n:06d}" for n in range(1, 1000)) if candidate not in nearby)


def _enable_totp(client: TestClient, clock: FixedClock) -> str:
    """Enrols and confirms TOTP through the routes, then moves the clock 60 s on. Returns the secret."""
    login = _session(client)
    assert login.status_code == 201
    token = str(login.json()["access_token"])
    enrolled = client.post(ENROL, headers=_bearer(token))
    assert enrolled.status_code == 201
    secret = str(enrolled.json()["secret"])
    confirmed = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock)})
    assert confirmed.status_code == 200
    clock.advance(timedelta(seconds=2 * STEP_SECONDS))
    return secret


# --- totp_code field -------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad_code", ["", "x" * 17, 123456, ["123456"]])
def test_user_tokens_rejects_a_totp_code_that_is_not_a_string_of_1_to_16_chars(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, bad_code: object
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = client.post(USER_TOKENS, json={"email": EMAIL, "password": PASSWORD, "totp_code": bad_code})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert "access_token" not in response.text


@pytest.mark.parametrize("length", [1, 16])
def test_user_tokens_accepts_a_totp_code_of_the_boundary_lengths(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, length: int
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _user_token(client, totp_code="7" * length)

    # Assert
    assert response.status_code == 201


def test_user_tokens_still_rejects_other_extra_fields_next_to_a_totp_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    body = {"email": EMAIL, "password": PASSWORD, "totp_code": "123456", "user_id": str(uuid.uuid4())}

    # Act
    response = client.post(USER_TOKENS, json=body)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert "access_token" not in response.text


# --- user without TOTP: unchanged ------------------------------------------------------------------------


def test_user_tokens_without_totp_issues_201_on_the_password_alone(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _user_token(client)

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {"access_token": body.get("access_token"), "token_type": "Bearer", "expires_in": 900}


def test_user_tokens_without_totp_ignores_a_garbage_totp_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _user_token(client, totp_code="garbage-code")

    # Assert
    assert response.status_code == 201
    assert response.json()["token_type"] == "Bearer"


def test_user_tokens_without_totp_works_when_no_encryption_key_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, totp_key=None)
    _sign_up(session_factory, clock)

    # Act
    response = _user_token(client)

    # Assert
    assert response.status_code == 201


# --- TOTP enabled ----------------------------------------------------------------------------------------


def test_user_tokens_asks_for_mfa_when_totp_is_enabled_and_no_code_is_given(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _enable_totp(client, clock)

    # Act
    response = _user_token(client)

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)


def test_user_tokens_with_the_right_code_issues_201_and_an_unchanged_ops_ai_token(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)

    # Act
    response = _user_token(client, totp_code=_code(secret, clock))

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {"access_token": body.get("access_token"), "token_type": "Bearer", "expires_in": 900}
    claims = jwt.decode(
        body["access_token"],
        _public_pem(private_pem),
        algorithms=["RS256"],
        audience="ops-ai",
        issuer="gateway",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert sorted(claims) == ["aud", "exp", "iat", "iss", "jti", "roles", "sub", "user_id"]
    assert claims["user_id"] == str(user_id)
    assert claims["sub"] == f"user:{user_id}"
    assert claims["roles"] == ["consumer"]
    assert claims["exp"] == int(clock().timestamp()) + 900


def test_user_tokens_with_a_wrong_code_answers_the_generic_invalid_credentials_and_no_token(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)

    # Act
    response = _user_token(client, totp_code=_wrong_code(secret, clock))

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)
    assert "access_token" not in response.text


def test_user_tokens_with_a_non_numeric_code_answers_the_generic_invalid_credentials(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _enable_totp(client, clock)

    # Act
    response = _user_token(client, totp_code="garbage-code")

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)


def test_user_tokens_rejects_a_replayed_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    code = _code(secret, clock)
    first = _user_token(client, totp_code=code)

    # Act
    second = _user_token(client, totp_code=code)

    # Assert
    assert (first.status_code, second.status_code, second.json()) == (201, 401, INVALID_CREDENTIALS)


def test_user_tokens_accepts_a_newer_code_after_the_previous_one_was_used(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    first = _user_token(client, totp_code=_code(secret, clock))

    # Act
    second = _user_token(client, totp_code=_code(secret, clock, 1))

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)


def test_user_tokens_answers_503_for_a_totp_user_when_no_encryption_key_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    keyed = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(keyed, clock)
    keyless = _client(session_factory, clock, private_pem, totp_key=None)

    # Act
    response = _user_token(keyless, totp_code=_code(secret, clock))

    # Assert
    assert response.status_code == 503
    assert "access_token" not in response.text


# --- lockout ---------------------------------------------------------------------------------------------


def test_user_tokens_locks_after_five_wrong_codes_even_for_a_correct_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    early = [_user_token(client, totp_code=_wrong_code(secret, clock)) for _ in range(MAX_FAILURES - 1)]
    _user_token(client, totp_code=_wrong_code(secret, clock))

    # Act
    response = _user_token(client, totp_code=_code(secret, clock))

    # Assert
    assert [(r.status_code, r.json()) for r in early] == [(401, INVALID_CREDENTIALS)] * (MAX_FAILURES - 1)
    assert (response.status_code, response.json()) == (423, MFA_LOCKED)


def test_user_tokens_shares_one_lockout_budget_with_sessions(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    sessions_failures = [_session(client, _wrong_code(secret, clock)) for _ in range(MAX_FAILURES - 1)]
    fifth = _user_token(client, totp_code=_wrong_code(secret, clock))

    # Act
    response = _user_token(client, totp_code=_code(secret, clock))

    # Assert
    assert [r.status_code for r in sessions_failures] == [401] * (MAX_FAILURES - 1)
    assert (fifth.status_code, fifth.json()) == (423, MFA_LOCKED)
    assert (response.status_code, response.json()) == (423, MFA_LOCKED)


def test_user_tokens_answers_423_after_the_lockout_was_reached_through_sessions(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    for _ in range(MAX_FAILURES):
        _session(client, _wrong_code(secret, clock))

    # Act
    response = _user_token(client, totp_code=_code(secret, clock))

    # Assert
    assert (response.status_code, response.json()) == (423, MFA_LOCKED)
    assert "access_token" not in response.text


def test_sessions_answers_423_after_the_lockout_was_reached_through_user_tokens(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    for _ in range(MAX_FAILURES):
        _user_token(client, totp_code=_wrong_code(secret, clock))

    # Act
    response = _session(client, _code(secret, clock))

    # Assert
    assert (response.status_code, response.json()) == (423, MFA_LOCKED)


def test_user_tokens_unlocks_after_fifteen_minutes(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    for _ in range(MAX_FAILURES):
        _user_token(client, totp_code=_wrong_code(secret, clock))
    clock.advance(timedelta(minutes=15, seconds=1))

    # Act
    response = _user_token(client, totp_code=_code(secret, clock))

    # Assert
    assert response.status_code == 201
    assert response.json()["token_type"] == "Bearer"


# --- replay across routes --------------------------------------------------------------------------------


def test_a_code_accepted_by_sessions_cannot_be_replayed_on_user_tokens(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    code = _code(secret, clock)
    accepted = _session(client, code)

    # Act
    replayed = _user_token(client, totp_code=code)

    # Assert
    assert accepted.status_code == 201
    assert (replayed.status_code, replayed.json()) == (401, INVALID_CREDENTIALS)


def test_a_code_accepted_by_user_tokens_cannot_be_replayed_on_sessions(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    code = _code(secret, clock)
    accepted = _user_token(client, totp_code=code)

    # Act
    replayed = _session(client, code)

    # Assert
    assert accepted.status_code == 201
    assert (replayed.status_code, replayed.json()) == (401, INVALID_CREDENTIALS)


# --- password first --------------------------------------------------------------------------------------


def test_user_tokens_with_a_wrong_password_for_a_totp_user_answers_invalid_credentials_not_mfa_required(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _enable_totp(client, clock)

    # Act
    response = _user_token(client, password=WRONG_PASSWORD)

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)


def test_user_tokens_with_a_wrong_password_neither_counts_a_totp_failure_nor_burns_the_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    code = _code(secret, clock)
    wrong_code = _wrong_code(secret, clock)
    for _ in range(MAX_FAILURES + 1):
        _user_token(client, password=WRONG_PASSWORD, totp_code=wrong_code)
    for _ in range(MAX_FAILURES + 1):
        _user_token(client, password=WRONG_PASSWORD, totp_code=code)

    # Act
    response = _user_token(client, totp_code=code)

    # Assert
    assert response.status_code == 201
    assert response.json()["token_type"] == "Bearer"


def test_user_tokens_with_a_wrong_password_writes_no_totp_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)

    # Act
    response = _user_token(client, password=WRONG_PASSWORD, totp_code=_wrong_code(secret, clock))

    # Assert
    totp_failures = [
        row for row in _audit(session_factory, "auth.login_failed") if row["summary"] == {"factor": "totp"}
    ]
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)
    assert totp_failures == []


# --- audit -----------------------------------------------------------------------------------------------


def test_a_wrong_totp_code_on_user_tokens_writes_auth_login_failed_for_the_totp_factor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)

    # Act
    _user_token(client, totp_code=_wrong_code(secret, clock))

    # Assert
    assert _audit(session_factory, "auth.login_failed") == [
        {
            "actor_id": f"user:{user_id}",
            "actor_role": "consumer",
            "action": "auth.login_failed",
            "summary": {"factor": "totp"},
        }
    ]


def test_a_replayed_totp_code_on_user_tokens_writes_auth_login_failed_for_the_totp_factor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    code = _code(secret, clock)
    _user_token(client, totp_code=code)

    # Act
    _user_token(client, totp_code=code)

    # Assert
    assert _audit(session_factory, "auth.login_failed") == [
        {
            "actor_id": f"user:{user_id}",
            "actor_role": "consumer",
            "action": "auth.login_failed",
            "summary": {"factor": "totp"},
        }
    ]


def test_a_missing_totp_code_on_user_tokens_writes_no_auth_login_failed_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _enable_totp(client, clock)

    # Act
    response = _user_token(client)

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)
    assert _audit(session_factory, "auth.login_failed") == []
