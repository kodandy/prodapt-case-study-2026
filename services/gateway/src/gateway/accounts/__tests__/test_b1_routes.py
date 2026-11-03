"""HTTP tests for the B1 routes, written from docs/specs/gateway-basic.md section B1 (Routes)."""

import json
import re
import threading
import uuid
from datetime import timedelta

import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.accounts.tokens import issue_merchant_token, issue_user_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.ledger.service import ensure_account
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.models import AccountBalance
from httpx import Response
from sqlalchemy import text, update
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

SESSIONS = "/v1/sessions"
ENROL = "/v1/me/mfa/totp"
VERIFY = "/v1/me/mfa/totp/verify"
WALLETS = "/v1/me/wallets"
EMAIL = "b1-user@example.com"
PASSWORD = "route-test-password-7Q"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
STEP_SECONDS = 30
MAX_FAILURES = 5
INVALID_CREDENTIALS = {"detail": "invalid credentials"}
MFA_REQUIRED = {"detail": "mfa required"}
MFA_LOCKED = {"detail": "mfa locked"}
INVALID_SESSION = {"detail": "invalid session"}
ME_ROUTES = [
    pytest.param("POST", ENROL, None, id="enrol"),
    pytest.param("POST", VERIFY, {"code": "123456"}, id="verify"),
    pytest.param("GET", WALLETS, None, id="wallets"),
]


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


@pytest.fixture(scope="module")
def other_private_pem() -> str:
    return _generate_private_pem()


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    *,
    totp_key: str | None = TOTP_KEY,
    login_slots: threading.BoundedSemaphore | None = None,
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
        login_slots=login_slots,
    )
    return TestClient(create_app(deps))


def _sign_up(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    email: str = EMAIL,
    password: str = PASSWORD,
    currency: str = "USD",
) -> tuple[uuid.UUID, uuid.UUID]:
    with session_factory() as session:
        result = sign_up(session, clock, email, password, currency=currency)
        session.commit()
    return result.user_id, result.wallet_account_id


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _audit(session_factory: sessionmaker[Session], *actions: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, summary FROM gateway.audit_log "
        "WHERE action = ANY(:actions) ORDER BY id",
        actions=list(actions),
    )


def _login(
    client: TestClient, email: str = EMAIL, password: str = PASSWORD, totp_code: str | None = None
) -> Response:
    body = {"email": email, "password": password}
    if totp_code is not None:
        body["totp_code"] = totp_code
    response: Response = client.post(SESSIONS, json=body)
    return response


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _session_token(client: TestClient, email: str = EMAIL, totp_code: str | None = None) -> str:
    response = _login(client, email=email, totp_code=totp_code)
    assert response.status_code == 201
    return str(response.json()["access_token"])


def _code(secret: str, clock: FixedClock, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()) + offset_steps * STEP_SECONDS)


def _wrong_code(secret: str, clock: FixedClock) -> str:
    nearby = {_code(secret, clock, offset) for offset in range(-3, 4)}
    return next(candidate for candidate in (f"{n:06d}" for n in range(1, 1000)) if candidate not in nearby)


def _enable_totp(client: TestClient, clock: FixedClock, email: str = EMAIL) -> tuple[str, str]:
    """Enrols and confirms TOTP, then moves the clock 60 s on. Returns (secret, password-only token)."""
    token = _session_token(client, email)
    enrolled = client.post(ENROL, headers=_bearer(token))
    assert enrolled.status_code == 201
    secret = str(enrolled.json()["secret"])
    confirmed = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock)})
    assert confirmed.status_code == 200
    clock.advance(timedelta(seconds=2 * STEP_SECONDS))
    return secret, token


def _otp_session(client: TestClient, clock: FixedClock, secret: str, offset_steps: int = 0) -> str:
    return _session_token(client, totp_code=_code(secret, clock, offset_steps))


def _call(
    client: TestClient, method: str, path: str, body: dict[str, str] | None, token: str | None
) -> Response:
    headers = {} if token is None else _bearer(token)
    response: Response = client.request(method, path, headers=headers, json=body)
    return response


# --- POST /v1/sessions: success --------------------------------------------------------------------------


def test_sessions_returns_201_with_a_gateway_audience_bearer_token_for_a_user_without_totp(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)

    # Act
    response = _login(client)

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "access_token": body.get("access_token"),
        "token_type": "Bearer",
        "expires_in": 900,
        "amr": ["pwd"],
    }
    claims = jwt.decode(
        body["access_token"],
        _public_pem(private_pem),
        algorithms=["RS256"],
        audience="gateway",
        issuer="gateway",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["sub"] == f"user:{user_id}"
    assert claims["user_id"] == str(user_id)
    assert claims["amr"] == ["pwd"]
    assert claims["iat"] == int(clock().timestamp())
    assert claims["exp"] == int(clock().timestamp()) + 900


def test_sessions_201_response_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _login(client)

    # Assert
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"


def test_sessions_matches_the_email_case_insensitively(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)

    # Act
    response = _login(client, email="B1-User@Example.COM")

    # Assert
    assert response.status_code == 201
    claims = jwt.decode(response.json()["access_token"], options={"verify_signature": False})
    assert claims["user_id"] == str(user_id)


def test_sessions_ignores_a_totp_code_when_totp_is_not_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _login(client, totp_code="000000")

    # Assert
    assert response.status_code == 201
    assert response.json()["amr"] == ["pwd"]


def test_sessions_works_for_a_user_without_totp_when_no_encryption_key_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, totp_key=None)
    _sign_up(session_factory, clock)

    # Act
    response = _login(client)

    # Assert
    assert response.status_code == 201
    assert response.json()["amr"] == ["pwd"]


# --- POST /v1/sessions: credentials ----------------------------------------------------------------------


def test_sessions_answers_unknown_email_and_wrong_password_with_the_identical_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    unknown = _login(client, email="nobody@example.com")
    wrong = _login(client, password="not the password")

    # Assert
    assert (unknown.status_code, unknown.json()) == (401, INVALID_CREDENTIALS)
    assert (wrong.status_code, wrong.json()) == (401, INVALID_CREDENTIALS)
    assert unknown.content == wrong.content


def test_sessions_with_wrong_password_for_a_totp_user_does_not_ask_for_mfa(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _enable_totp(client, clock)

    # Act
    response = _login(client, password="not the password")

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)


def test_sessions_with_wrong_password_neither_counts_a_totp_failure_nor_burns_the_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    code = _code(secret, clock)
    for _ in range(MAX_FAILURES + 1):
        _login(client, password="not the password", totp_code=code)

    # Act
    response = _login(client, totp_code=code)

    # Assert
    assert response.status_code == 201
    assert response.json()["amr"] == ["pwd", "otp"]


def test_sessions_answers_503_with_retry_after_when_no_login_slot_frees_up(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False) is True
    client = _client(session_factory, clock, private_pem, login_slots=slots)
    _sign_up(session_factory, clock)

    # Act
    response = _login(client)

    # Assert
    assert response.status_code == 503
    assert response.json() == {"detail": "try again later"}
    assert response.headers["Retry-After"] == "1"


# --- POST /v1/sessions: totp_code field ------------------------------------------------------------------


@pytest.mark.parametrize("bad_code", ["", "x" * 17, 123456, ["123456"]])
def test_sessions_rejects_a_totp_code_that_is_not_a_string_of_1_to_16_chars(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, bad_code: object
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = client.post(SESSIONS, json={"email": EMAIL, "password": PASSWORD, "totp_code": bad_code})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"


@pytest.mark.parametrize("length", [1, 16])
def test_sessions_accepts_a_totp_code_of_the_boundary_lengths(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, length: int
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _login(client, totp_code="7" * length)

    # Assert
    assert response.status_code == 201


# --- POST /v1/sessions: TOTP enabled ---------------------------------------------------------------------


def test_sessions_asks_for_mfa_when_totp_is_enabled_and_no_code_is_given(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _enable_totp(client, clock)

    # Act
    response = _login(client)

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)


def test_sessions_with_the_right_code_issues_a_pwd_and_otp_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)

    # Act
    response = _login(client, totp_code=_code(secret, clock))

    # Assert
    body = response.json()
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    assert response.status_code == 201
    assert body["amr"] == ["pwd", "otp"]
    assert claims["amr"] == ["pwd", "otp"]


def test_sessions_with_a_wrong_code_answers_the_generic_invalid_credentials(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)

    # Act
    response = _login(client, totp_code=_wrong_code(secret, clock))

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)


def test_sessions_rejects_a_replayed_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    code = _code(secret, clock)
    first = _login(client, totp_code=code)

    # Act
    second = _login(client, totp_code=code)

    # Assert
    assert (first.status_code, second.status_code, second.json()) == (201, 401, INVALID_CREDENTIALS)


def test_sessions_accepts_a_new_code_after_the_previous_one_was_used(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    first = _login(client, totp_code=_code(secret, clock))

    # Act
    second = _login(client, totp_code=_code(secret, clock, 1))

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)


def test_sessions_locks_after_five_wrong_codes_even_for_a_correct_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    early = [_login(client, totp_code=_wrong_code(secret, clock)) for _ in range(MAX_FAILURES - 1)]
    _login(client, totp_code=_wrong_code(secret, clock))

    # Act
    response = _login(client, totp_code=_code(secret, clock))

    # Assert
    assert [(r.status_code, r.json()) for r in early] == [(401, INVALID_CREDENTIALS)] * (MAX_FAILURES - 1)
    assert (response.status_code, response.json()) == (423, MFA_LOCKED)


def test_sessions_unlocks_after_fifteen_minutes(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    for _ in range(MAX_FAILURES):
        _login(client, totp_code=_wrong_code(secret, clock))
    clock.advance(timedelta(minutes=15, seconds=1))

    # Act
    response = _login(client, totp_code=_code(secret, clock))

    # Assert
    assert response.status_code == 201
    assert response.json()["amr"] == ["pwd", "otp"]


def test_sessions_answers_503_for_a_totp_user_when_no_encryption_key_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    keyed = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(keyed, clock)
    keyless = _client(session_factory, clock, private_pem, totp_key=None)

    # Act
    response = _login(keyless, totp_code=_code(secret, clock))

    # Assert
    assert response.status_code == 503


# --- POST /v1/sessions: audit ----------------------------------------------------------------------------


def test_a_successful_login_writes_an_auth_login_row_with_the_amr(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)

    # Act
    _login(client)

    # Assert
    assert _audit(session_factory, "auth.login") == [
        {
            "actor_id": f"user:{user_id}",
            "actor_role": "consumer",
            "action": "auth.login",
            "summary": {"amr": ["pwd"]},
        }
    ]


def test_a_totp_login_writes_an_auth_login_row_with_both_factors(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    before = len(_audit(session_factory, "auth.login"))

    # Act
    _login(client, totp_code=_code(secret, clock))

    # Assert
    rows = _audit(session_factory, "auth.login")
    assert len(rows) - before == 1
    assert (rows[-1]["actor_id"], rows[-1]["summary"]) == (f"user:{user_id}", {"amr": ["pwd", "otp"]})


def test_a_wrong_password_for_a_known_user_writes_auth_login_failed_for_the_password_factor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)

    # Act
    _login(client, password="not the password")

    # Assert
    assert _audit(session_factory, "auth.login", "auth.login_failed") == [
        {
            "actor_id": f"user:{user_id}",
            "actor_role": "consumer",
            "action": "auth.login_failed",
            "summary": {"factor": "password"},
        }
    ]


def test_a_wrong_totp_code_writes_auth_login_failed_for_the_totp_factor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    before = len(_audit(session_factory, "auth.login"))

    # Act
    _login(client, totp_code=_wrong_code(secret, clock))

    # Assert
    failed = _audit(session_factory, "auth.login_failed")
    assert failed == [
        {
            "actor_id": f"user:{user_id}",
            "actor_role": "consumer",
            "action": "auth.login_failed",
            "summary": {"factor": "totp"},
        }
    ]
    assert len(_audit(session_factory, "auth.login")) == before


def test_an_unknown_email_writes_no_audit_row_at_all(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _login(client, email="nobody@example.com")

    # Assert
    assert response.status_code == 401
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.audit_log") == [{"n": 0}]


def test_audit_rows_never_contain_the_email_the_password_or_any_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    codes = [_wrong_code(secret, clock)] + [_code(secret, clock, offset) for offset in (-1, 0, 1)]
    _login(client, password="wrong-password-xyz")
    _login(client, totp_code=codes[0])
    _login(client, totp_code=codes[2])

    # Act
    rows = _rows(
        session_factory, "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log"
    )

    # Assert
    serialized = json.dumps(rows, default=str)
    assert len(rows) > 0
    for forbidden in (EMAIL, PASSWORD, "wrong-password-xyz", secret, *codes):
        assert forbidden not in serialized


def test_a_lockout_through_login_writes_one_mfa_totp_locked_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)

    # Act
    for _ in range(MAX_FAILURES):
        _login(client, totp_code=_wrong_code(secret, clock))

    # Assert
    locked = _audit(session_factory, "mfa.totp_locked")
    assert [(r["actor_id"], r["actor_role"]) for r in locked] == [(f"user:{user_id}", "consumer")]


# --- POST /v1/me/mfa/totp --------------------------------------------------------------------------------


def test_enrol_returns_201_with_a_secret_and_an_otpauth_uri_and_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    response = client.post(ENROL, headers=_bearer(token))

    # Assert
    body = response.json()
    parsed = pyotp.parse_uri(body["otpauth_uri"])
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert sorted(body) == ["otpauth_uri", "secret"]
    assert re.fullmatch(r"[A-Z2-7]{32}", body["secret"]) is not None
    assert isinstance(parsed, pyotp.TOTP)
    assert (parsed.secret, parsed.issuer) == (body["secret"], "PayPlat")


def test_enrol_stores_the_secret_encrypted_and_leaves_totp_pending(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    secret = client.post(ENROL, headers=_bearer(token)).json()["secret"]

    # Assert
    rows = _rows(session_factory, "SELECT user_id, secret_ciphertext, confirmed_at FROM gateway.user_totp")
    assert [(r["user_id"], r["confirmed_at"]) for r in rows] == [(user_id, None)]
    stored = rows[0]["secret_ciphertext"]
    assert isinstance(stored, bytes | memoryview)
    assert secret.encode() not in bytes(stored)


def test_enrolling_again_before_confirmation_returns_a_new_secret(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)
    first = client.post(ENROL, headers=_bearer(token))

    # Act
    second = client.post(ENROL, headers=_bearer(token))

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["secret"] != second.json()["secret"]


def test_enrol_answers_409_when_totp_is_already_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    token = _otp_session(client, clock, secret)

    # Act
    response = client.post(ENROL, headers=_bearer(token))

    # Assert
    assert response.status_code == 409


def test_enrol_answers_503_without_an_encryption_key_and_stores_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, totp_key=None)
    _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    response = client.post(ENROL, headers=_bearer(token))

    # Assert
    assert response.status_code == 503
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.user_totp") == [{"n": 0}]


# --- POST /v1/me/mfa/totp/verify -------------------------------------------------------------------------


def test_verify_with_the_current_code_enables_totp(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)
    secret = client.post(ENROL, headers=_bearer(token)).json()["secret"]

    # Act
    response = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock)})

    # Assert
    assert (response.status_code, response.json()) == (200, {"totp_enabled": True})
    assert _rows(session_factory, "SELECT confirmed_at FROM gateway.user_totp") == [{"confirmed_at": clock()}]


def test_verify_with_a_wrong_code_answers_400_and_leaves_totp_disabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)
    secret = client.post(ENROL, headers=_bearer(token)).json()["secret"]

    # Act
    response = client.post(VERIFY, headers=_bearer(token), json={"code": _wrong_code(secret, clock)})

    # Assert
    assert (response.status_code, response.json()) == (400, {"detail": "invalid code"})
    assert _rows(session_factory, "SELECT confirmed_at FROM gateway.user_totp") == [{"confirmed_at": None}]
    assert _login(client).json()["amr"] == ["pwd"]


def test_verify_without_an_enrolment_answers_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    response = client.post(VERIFY, headers=_bearer(token), json={"code": "123456"})

    # Assert
    assert (response.status_code, response.json()) == (404, {"detail": "totp not enrolled"})


def test_verify_answers_409_when_totp_is_already_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    token = _otp_session(client, clock, secret)

    # Act
    response = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock, 1)})

    # Assert
    assert response.status_code == 409


def test_verify_answers_423_after_five_wrong_codes_even_for_the_correct_one(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)
    secret = client.post(ENROL, headers=_bearer(token)).json()["secret"]
    early = [
        client.post(VERIFY, headers=_bearer(token), json={"code": _wrong_code(secret, clock)})
        for _ in range(MAX_FAILURES - 1)
    ]
    client.post(VERIFY, headers=_bearer(token), json={"code": _wrong_code(secret, clock)})

    # Act
    response = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock)})

    # Assert
    assert [(r.status_code, r.json()) for r in early] == [(400, {"detail": "invalid code"})] * (
        MAX_FAILURES - 1
    )
    assert response.status_code == 423
    assert _rows(session_factory, "SELECT confirmed_at FROM gateway.user_totp") == [{"confirmed_at": None}]


def test_verify_answers_503_without_an_encryption_key(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, totp_key=None)
    _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    response = client.post(VERIFY, headers=_bearer(token), json={"code": "123456"})

    # Assert
    assert response.status_code == 503


def test_enrolling_and_verifying_write_the_mfa_audit_rows(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)

    # Act
    secret, _ = _enable_totp(client, clock)

    # Assert
    rows = _audit(session_factory, "mfa.totp_enrolled", "mfa.totp_enabled")
    assert [(r["actor_id"], r["actor_role"], r["action"]) for r in rows] == [
        (f"user:{user_id}", "consumer", "mfa.totp_enrolled"),
        (f"user:{user_id}", "consumer", "mfa.totp_enabled"),
    ]
    assert secret not in json.dumps(rows, default=str)


# --- Bearer session on /v1/me/* --------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), ME_ROUTES)
def test_me_routes_answer_401_missing_session_without_a_bearer(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    response = _call(client, method, path, body, None)

    # Assert
    assert (response.status_code, response.json()) == (401, {"detail": "missing session"})


@pytest.mark.parametrize(("method", "path", "body"), ME_ROUTES)
@pytest.mark.parametrize(
    "kind", ["ops_ai_user_token", "merchant_token", "api_key", "garbage", "expired", "other_key", "malformed"]
)
def test_me_routes_answer_401_invalid_session_for_anything_but_a_live_gateway_session(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    other_private_pem: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
    kind: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, _ = _sign_up(session_factory, clock)
    long_ago = clock() - timedelta(hours=1)
    tokens = {
        "ops_ai_user_token": issue_user_token(private_pem, user_id, clock()),
        "merchant_token": issue_merchant_token(private_pem, uuid.uuid4(), clock()),
        "api_key": "sk_test_abcdefghijklmnopqrstuvwxyz0123456789",
        "garbage": "not-a-jwt",
        "expired": issue_session_token(private_pem, user_id, long_ago, ("pwd",)),
        "other_key": issue_session_token(other_private_pem, user_id, clock(), ("pwd",)),
        "malformed": "x.y.z",
    }

    # Act
    response = _call(client, method, path, body, tokens[kind])

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_SESSION)


def test_a_session_stops_working_once_its_900_seconds_are_over(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)
    before = client.get(WALLETS, headers=_bearer(token))
    clock.advance(timedelta(seconds=901))

    # Act
    after = client.get(WALLETS, headers=_bearer(token))

    # Assert
    assert before.status_code == 200
    assert (after.status_code, after.json()) == (401, INVALID_SESSION)


@pytest.mark.parametrize(("method", "path", "body"), ME_ROUTES)
def test_me_routes_require_an_otp_session_once_totp_is_enabled(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _, password_only_token = _enable_totp(client, clock)

    # Act
    response = _call(client, method, path, body, password_only_token)

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)


def test_wallets_accepts_the_otp_session_once_totp_is_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, wallet_id = _sign_up(session_factory, clock)
    secret, _ = _enable_totp(client, clock)
    token = _otp_session(client, clock, secret)

    # Act
    response = client.get(WALLETS, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert [item["account_id"] for item in response.json()["items"]] == [str(wallet_id)]


def test_a_session_for_a_user_without_totp_may_hold_only_the_password_factor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    response = client.get(WALLETS, headers=_bearer(token))

    # Assert
    assert response.status_code == 200


# --- GET /v1/me/wallets ----------------------------------------------------------------------------------


def test_wallets_returns_the_signup_wallet_with_a_zero_balance(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, wallet_id = _sign_up(session_factory, clock)
    token = _session_token(client)

    # Act
    response = client.get(WALLETS, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "items": [{"account_id": str(wallet_id), "currency": "USD", "available_minor": 0, "held_minor": 0}]
    }


def test_wallets_returns_balances_ordered_by_currency(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id, usd_id = _sign_up(session_factory, clock)
    with session_factory.begin() as session:
        gbp_id = ensure_account(session, "user", user_id, "wallet", "GBP")
        eur_id = ensure_account(session, "user", user_id, "wallet", "EUR")
        session.add(AccountBalance(account_id=gbp_id, available_minor=900, held_minor=9))
        session.add(AccountBalance(account_id=eur_id, available_minor=700, held_minor=7))
        session.execute(
            update(AccountBalance)
            .where(AccountBalance.account_id == usd_id)
            .values(available_minor=12_345, held_minor=678)
        )
    token = _session_token(client)

    # Act
    response = client.get(WALLETS, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"account_id": str(eur_id), "currency": "EUR", "available_minor": 700, "held_minor": 7},
            {"account_id": str(gbp_id), "currency": "GBP", "available_minor": 900, "held_minor": 9},
            {"account_id": str(usd_id), "currency": "USD", "available_minor": 12_345, "held_minor": 678},
        ]
    }


def test_wallets_never_shows_another_users_wallets(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, alice_wallet = _sign_up(session_factory, clock, email="alice@example.com")
    _, bob_wallet = _sign_up(session_factory, clock, email="bob@example.com", currency="EUR")
    token = _session_token(client, email="bob@example.com")

    # Act
    response = client.get(WALLETS, headers=_bearer(token))

    # Assert
    items = response.json()["items"]
    assert response.status_code == 200
    assert [(item["account_id"], item["currency"]) for item in items] == [(str(bob_wallet), "EUR")]
    assert str(alice_wallet) not in response.text
