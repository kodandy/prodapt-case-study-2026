"""Tests for the per-account password throttle, written from docs/specs/gateway-basic.md (B1, Decision 7)."""

import threading
import uuid
from datetime import timedelta

import pyotp
import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.models import LoginThrottle
from gateway.accounts.service import sign_up
from gateway.accounts.throttle import LOGIN_FAILURE_WINDOW, LOGIN_LOCKOUT, LOGIN_MAX_FAILURES
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from httpx import Response
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

SESSIONS = "/v1/sessions"
USER_TOKENS = "/v1/user-tokens"
ENROL = "/v1/me/mfa/totp"
VERIFY = "/v1/me/mfa/totp/verify"
EMAIL = "decision7-user@example.com"
OTHER_EMAIL = "decision7-other@example.com"
UNKNOWN_EMAIL = "decision7-nobody@example.com"
PASSWORD = "route-test-password-7Q"
WRONG_PASSWORD = "not-the-password-canary-81"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
STEP_SECONDS = 30
INVALID_CREDENTIALS = {"detail": "invalid credentials"}
TOO_MANY_ATTEMPTS = {"detail": "too many attempts"}
LOCKOUT_SECONDS = int(LOGIN_LOCKOUT.total_seconds())
BOTH_ROUTES = [pytest.param(SESSIONS, id="sessions"), pytest.param(USER_TOKENS, id="user-tokens")]


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    *,
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
        totp_encryption_key=TOTP_KEY,
        login_slots=login_slots,
    )
    return TestClient(create_app(deps))


def _sign_up(session_factory: sessionmaker[Session], clock: FixedClock, email: str = EMAIL) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _login(
    client: TestClient,
    route: str,
    *,
    email: str = EMAIL,
    password: str = PASSWORD,
    totp_code: str | None = None,
) -> Response:
    body = {"email": email, "password": password}
    if totp_code is not None:
        body["totp_code"] = totp_code
    response: Response = client.post(route, json=body)
    return response


def _fail(client: TestClient, route: str, times: int, email: str = EMAIL) -> list[Response]:
    return [_login(client, route, email=email, password=WRONG_PASSWORD) for _ in range(times)]


def _lock(client: TestClient, route: str, email: str = EMAIL) -> None:
    for response in _fail(client, route, LOGIN_MAX_FAILURES, email):
        assert response.status_code == 401


def _throttle_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            {
                "user_id": row.user_id,
                "failed_attempts": row.failed_attempts,
                "window_started_at": row.window_started_at,
                "locked_until": row.locked_until,
                "updated_at": row.updated_at,
            }
            for row in session.scalars(select(LoginThrottle).order_by(LoginThrottle.user_id))
        ]


def _failed_attempts(session_factory: sessionmaker[Session]) -> list[int]:
    return [int(str(row["failed_attempts"])) for row in _throttle_rows(session_factory)]


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


def _throttle_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.login_throttle")).scalar_one())


def _audit_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM gateway.audit_log")).scalar_one())


def _totp_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT user_id, confirmed_at, last_used_step, failed_attempts, locked_until "
                "FROM gateway.user_totp ORDER BY user_id"
            )
        ).mappings()
        return [dict(row) for row in rows]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _code(secret: str, clock: FixedClock, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()) + offset_steps * STEP_SECONDS)


def _wrong_code(secret: str, clock: FixedClock) -> str:
    nearby = {_code(secret, clock, offset) for offset in range(-3, 4)}
    return next(candidate for candidate in (f"{n:06d}" for n in range(1, 1000)) if candidate not in nearby)


def _enable_totp(client: TestClient, clock: FixedClock) -> str:
    """Enrols and confirms TOTP through the routes, then moves the clock 60 s on. Returns the secret."""
    login = _login(client, SESSIONS)
    assert login.status_code == 201
    token = str(login.json()["access_token"])
    enrolled = client.post(ENROL, headers=_bearer(token))
    assert enrolled.status_code == 201
    secret = str(enrolled.json()["secret"])
    confirmed = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock)})
    assert confirmed.status_code == 200
    clock.advance(timedelta(seconds=2 * STEP_SECONDS))
    return secret


def _count_argon2_verifications(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original_verify = PasswordHasher.verify

    def counting_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        calls.append("verify")
        return original_verify(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting_verify)
    return calls


class _SignallingSlots(threading.BoundedSemaphore):
    """A login-slot semaphore that sets `waiting` each time a request calls acquire, before it can block."""

    def __init__(self, value: int, waiting: threading.Event) -> None:
        super().__init__(value)
        self._waiting = waiting

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        self._waiting.set()
        return super().acquire(blocking, timeout)


def _write_lock(session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID) -> None:
    """Writes the row another request's tenth failure would leave, in its own committed transaction."""
    with session_factory() as session:
        session.merge(
            LoginThrottle(
                user_id=user_id,
                failed_attempts=LOGIN_MAX_FAILURES,
                window_started_at=clock(),
                locked_until=clock() + LOGIN_LOCKOUT,
                updated_at=clock(),
            )
        )
        session.commit()


def _locked_row(user_id: uuid.UUID, clock: FixedClock) -> dict[str, object]:
    return {
        "user_id": user_id,
        "failed_attempts": LOGIN_MAX_FAILURES,
        "window_started_at": clock(),
        "locked_until": clock() + LOGIN_LOCKOUT,
        "updated_at": clock(),
    }


# --- the constants ---------------------------------------------------------------------------------------


def test_the_throttle_allows_10_failures_in_a_15_minute_window_and_locks_for_15_minutes() -> None:
    # Arrange / Act
    constants = (LOGIN_MAX_FAILURES, LOGIN_FAILURE_WINDOW, LOGIN_LOCKOUT)

    # Assert
    assert constants == (10, timedelta(minutes=15), timedelta(minutes=15))


# --- nine failures do not lock; the tenth does ---------------------------------------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_nine_wrong_passwords_leave_the_account_unlocked_so_the_correct_password_logs_in(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    failures = _fail(client, route, LOGIN_MAX_FAILURES - 1)

    # Act
    response = _login(client, route)

    # Assert
    assert [(r.status_code, r.json()) for r in failures] == [(401, INVALID_CREDENTIALS)] * (
        LOGIN_MAX_FAILURES - 1
    )
    assert response.status_code == 201
    assert response.json()["token_type"] == "Bearer"
    assert _audit(session_factory, "auth.login_locked") == []


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_the_tenth_wrong_password_still_answers_the_identical_401_and_writes_one_login_locked_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _fail(client, route, LOGIN_MAX_FAILURES - 1)

    # Act
    tenth = _login(client, route, password=WRONG_PASSWORD)

    # Assert
    assert (tenth.status_code, tenth.json()) == (401, INVALID_CREDENTIALS)
    assert "Retry-After" not in tenth.headers
    assert _audit(session_factory, "auth.login_locked") == [
        {
            "actor_id": f"user:{user_id}",
            "actor_role": "consumer",
            "action": "auth.login_locked",
            "summary": {},
        }
    ]


def test_the_tenth_failure_stores_the_lock_and_the_window_in_the_throttle_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)

    # Act
    _lock(client, SESSIONS)

    # Assert
    assert _throttle_rows(session_factory) == [
        {
            "user_id": user_id,
            "failed_attempts": LOGIN_MAX_FAILURES,
            "window_started_at": clock(),
            "locked_until": clock() + LOGIN_LOCKOUT,
            "updated_at": clock(),
        }
    ]


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_a_locked_account_answers_429_too_many_attempts_even_for_the_correct_password(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _lock(client, route)

    # Act
    response = _login(client, route)

    # Assert
    assert (response.status_code, response.json()) == (429, TOO_MANY_ATTEMPTS)
    assert response.headers["Retry-After"] == str(LOCKOUT_SECONDS)
    assert "access_token" not in response.text


def test_a_locked_account_answers_the_same_429_for_a_wrong_and_a_correct_password(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _lock(client, SESSIONS)

    # Act
    correct = _login(client, SESSIONS)
    wrong = _login(client, SESSIONS, password=WRONG_PASSWORD)

    # Assert
    assert (correct.status_code, correct.json()) == (429, TOO_MANY_ATTEMPTS)
    assert (wrong.status_code, wrong.json()) == (429, TOO_MANY_ATTEMPTS)
    assert correct.content == wrong.content
    assert correct.headers["Retry-After"] == wrong.headers["Retry-After"] == str(LOCKOUT_SECONDS)


@pytest.mark.parametrize(
    ("elapsed", "expected_retry_after"),
    [
        pytest.param(timedelta(seconds=0), "900", id="just-locked"),
        pytest.param(timedelta(seconds=100), "800", id="100s-later"),
        pytest.param(LOGIN_LOCKOUT - timedelta(seconds=1), "1", id="one-second-left"),
        pytest.param(LOGIN_LOCKOUT - timedelta(milliseconds=500), "1", id="half-a-second-left-is-at-least-1"),
    ],
)
def test_retry_after_is_the_whole_seconds_left_of_the_lock_and_at_least_1(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    elapsed: timedelta,
    expected_retry_after: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _lock(client, SESSIONS)
    clock.advance(elapsed)

    # Act
    response = _login(client, SESSIONS)

    # Assert
    assert (response.status_code, response.json()) == (429, TOO_MANY_ATTEMPTS)
    assert response.headers["Retry-After"] == expected_retry_after


# --- a locked request is cheap and side-effect free ------------------------------------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_a_locked_request_runs_no_argon2_verification(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    route: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _lock(client, route)
    calls = _count_argon2_verifications(monkeypatch)

    # Act
    correct = _login(client, route)
    wrong = _login(client, route, password=WRONG_PASSWORD)

    # Assert
    assert (correct.status_code, wrong.status_code) == (429, 429)
    assert calls == []


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_a_locked_request_adds_no_failure_and_writes_no_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _lock(client, route)
    throttle_before = _throttle_rows(session_factory)
    audit_rows_before = _audit_count(session_factory)

    # Act
    correct = _login(client, route)
    wrong = _login(client, route, password=WRONG_PASSWORD)

    # Assert
    assert (correct.status_code, wrong.status_code) == (429, 429)
    assert _throttle_rows(session_factory) == throttle_before
    assert _failed_attempts(session_factory) == [LOGIN_MAX_FAILURES]
    assert _audit_count(session_factory) == audit_rows_before
    assert len(_audit(session_factory, "auth.login_failed")) == LOGIN_MAX_FAILURES
    assert len(_audit(session_factory, "auth.login_locked")) == 1


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_a_locked_request_does_not_take_a_login_slot(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    slots = threading.BoundedSemaphore(1)
    client = _client(session_factory, clock, private_pem, login_slots=slots)
    _sign_up(session_factory, clock)
    _lock(client, route)
    assert slots.acquire(blocking=False) is True

    # Act
    response = _login(client, route)

    # Assert
    assert (response.status_code, response.json()) == (429, TOO_MANY_ATTEMPTS)
    assert response.headers["Retry-After"] == str(LOCKOUT_SECONDS)


# --- one counter for both routes -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first_route", "second_route"),
    [
        pytest.param(SESSIONS, USER_TOKENS, id="sessions-then-user-tokens"),
        pytest.param(USER_TOKENS, SESSIONS, id="user-tokens-then-sessions"),
    ],
)
def test_failures_on_both_routes_share_one_counter_and_the_lock_applies_to_both(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    first_route: str,
    second_route: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    half = LOGIN_MAX_FAILURES // 2
    first = _fail(client, first_route, half)
    second = _fail(client, second_route, LOGIN_MAX_FAILURES - half)

    # Act
    on_first = _login(client, first_route)
    on_second = _login(client, second_route)

    # Assert
    assert [r.status_code for r in first + second] == [401] * LOGIN_MAX_FAILURES
    assert _failed_attempts(session_factory) == [LOGIN_MAX_FAILURES]
    assert (on_first.status_code, on_first.json()) == (429, TOO_MANY_ATTEMPTS)
    assert (on_second.status_code, on_second.json()) == (429, TOO_MANY_ATTEMPTS)
    assert len(_audit(session_factory, "auth.login_locked")) == 1


def test_a_lock_on_one_account_does_not_lock_another_account(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _sign_up(session_factory, clock, OTHER_EMAIL)
    _lock(client, SESSIONS)

    # Act
    other = _login(client, SESSIONS, email=OTHER_EMAIL)

    # Assert
    assert other.status_code == 201
    assert _failed_attempts(session_factory) == [LOGIN_MAX_FAILURES]


# --- lock expiry -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_the_correct_password_succeeds_after_the_lockout_and_the_counter_is_back_to_zero(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _lock(client, route)
    clock.advance(LOGIN_LOCKOUT + timedelta(seconds=1))

    # Act
    response = _login(client, route)

    # Assert
    assert response.status_code == 201
    assert response.json()["token_type"] == "Bearer"
    rows = _throttle_rows(session_factory)
    assert [(row["user_id"], row["failed_attempts"], row["locked_until"]) for row in rows] == [
        (user_id, 0, None)
    ]


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_after_the_lock_expires_nine_more_failures_do_not_lock_and_the_tenth_does(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _lock(client, route)
    clock.advance(LOGIN_LOCKOUT + timedelta(seconds=1))

    # Act
    nine = _fail(client, route, LOGIN_MAX_FAILURES - 1)
    after_nine = _failed_attempts(session_factory)
    tenth = _login(client, route, password=WRONG_PASSWORD)
    locked = _login(client, route)

    # Assert
    assert [(r.status_code, r.json()) for r in nine] == [(401, INVALID_CREDENTIALS)] * (
        LOGIN_MAX_FAILURES - 1
    )
    assert after_nine == [LOGIN_MAX_FAILURES - 1]
    assert (tenth.status_code, tenth.json()) == (401, INVALID_CREDENTIALS)
    assert (locked.status_code, locked.json()) == (429, TOO_MANY_ATTEMPTS)
    assert locked.headers["Retry-After"] == str(LOCKOUT_SECONDS)
    assert len(_audit(session_factory, "auth.login_locked")) == 2


# --- the failure window ----------------------------------------------------------------------------------


def test_nine_failures_then_the_window_passes_then_nine_more_do_not_lock(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _fail(client, SESSIONS, LOGIN_MAX_FAILURES - 1)
    clock.advance(LOGIN_FAILURE_WINDOW + timedelta(seconds=1))

    # Act
    later = _fail(client, SESSIONS, LOGIN_MAX_FAILURES - 1)
    correct = _login(client, SESSIONS)

    # Assert
    assert [(r.status_code, r.json()) for r in later] == [(401, INVALID_CREDENTIALS)] * (
        LOGIN_MAX_FAILURES - 1
    )
    assert correct.status_code == 201
    assert _audit(session_factory, "auth.login_locked") == []
    assert _throttle_rows(session_factory)[0]["user_id"] == user_id


def test_a_failure_after_the_window_starts_a_new_window_at_one(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _fail(client, SESSIONS, LOGIN_MAX_FAILURES - 1)
    clock.advance(LOGIN_FAILURE_WINDOW + timedelta(seconds=1))

    # Act
    response = _login(client, SESSIONS, password=WRONG_PASSWORD)

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_CREDENTIALS)
    assert _throttle_rows(session_factory) == [
        {
            "user_id": user_id,
            "failed_attempts": 1,
            "window_started_at": clock(),
            "locked_until": None,
            "updated_at": clock(),
        }
    ]


def test_failures_inside_the_window_keep_counting_and_the_tenth_locks(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    window_start = clock()
    _fail(client, SESSIONS, LOGIN_MAX_FAILURES - 1)
    clock.advance(LOGIN_FAILURE_WINDOW - timedelta(seconds=1))

    # Act
    tenth = _login(client, SESSIONS, password=WRONG_PASSWORD)
    locked = _login(client, SESSIONS)

    # Assert
    assert (tenth.status_code, tenth.json()) == (401, INVALID_CREDENTIALS)
    assert (locked.status_code, locked.json()) == (429, TOO_MANY_ATTEMPTS)
    assert _throttle_rows(session_factory) == [
        {
            "user_id": user_id,
            "failed_attempts": LOGIN_MAX_FAILURES,
            "window_started_at": window_start,
            "locked_until": clock() + LOGIN_LOCKOUT,
            "updated_at": clock(),
        }
    ]


# --- a password success resets the counter ---------------------------------------------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_nine_failures_a_success_and_nine_more_failures_do_not_lock(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    _fail(client, route, LOGIN_MAX_FAILURES - 1)
    success = _login(client, route)

    # Act
    later = _fail(client, route, LOGIN_MAX_FAILURES - 1)
    correct = _login(client, route)

    # Assert
    assert success.status_code == 201
    assert [(r.status_code, r.json()) for r in later] == [(401, INVALID_CREDENTIALS)] * (
        LOGIN_MAX_FAILURES - 1
    )
    assert correct.status_code == 201
    assert _audit(session_factory, "auth.login_locked") == []


def test_a_success_after_failures_resets_the_row_to_zero_failures_and_no_lock(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _fail(client, SESSIONS, LOGIN_MAX_FAILURES - 1)

    # Act
    response = _login(client, SESSIONS)

    # Assert
    rows = _throttle_rows(session_factory)
    assert response.status_code == 201
    assert [(row["user_id"], row["failed_attempts"], row["locked_until"]) for row in rows] == [
        (user_id, 0, None)
    ]


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_a_success_with_no_prior_failures_writes_no_throttle_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    responses = [_login(client, route), _login(client, route)]

    # Assert
    assert [r.status_code for r in responses] == [201, 201]
    assert _throttle_count(session_factory) == 0


# --- unknown emails --------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
def test_unknown_emails_are_never_throttled_and_write_no_throttle_or_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, route: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    attempts = LOGIN_MAX_FAILURES + 5

    # Act
    responses = _fail(client, route, attempts, email=UNKNOWN_EMAIL)

    # Assert
    assert [(r.status_code, r.json()) for r in responses] == [(401, INVALID_CREDENTIALS)] * attempts
    assert all("Retry-After" not in r.headers for r in responses)
    assert _throttle_count(session_factory) == 0
    assert _audit_count(session_factory) == 0


# --- count and audit row stay consistent -----------------------------------------------------------------


@pytest.mark.parametrize("failures", [1, 4, LOGIN_MAX_FAILURES - 1])
def test_the_failure_count_equals_the_number_of_password_login_failed_audit_rows(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, failures: int
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    routes = [SESSIONS, USER_TOKENS]

    # Act
    responses = [_login(client, routes[index % 2], password=WRONG_PASSWORD) for index in range(failures)]

    # Assert
    assert [r.status_code for r in responses] == [401] * failures
    assert _failed_attempts(session_factory) == [failures]
    assert (
        _audit(session_factory, "auth.login_failed")
        == [
            {
                "actor_id": f"user:{user_id}",
                "actor_role": "consumer",
                "action": "auth.login_failed",
                "summary": {"factor": "password"},
            }
        ]
        * failures
    )


def test_the_failure_count_equals_the_login_failed_rows_at_the_lock(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)

    # Act
    _lock(client, USER_TOKENS)

    # Assert
    assert _failed_attempts(session_factory) == [LOGIN_MAX_FAILURES]
    assert len(_audit(session_factory, "auth.login_failed")) == LOGIN_MAX_FAILURES


# --- TOTP: the lock comes first --------------------------------------------------------------------------


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize("code_kind", ["wrong", "correct"])
def test_a_locked_totp_user_gets_429_before_any_totp_check_and_no_totp_failure_is_counted(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    route: str,
    code_kind: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock)
    secret = _enable_totp(client, clock)
    _lock(client, SESSIONS)
    totp_before = _totp_rows(session_factory)
    totp_login_failures_before = len(_audit(session_factory, "auth.login_failed"))
    code = _wrong_code(secret, clock) if code_kind == "wrong" else _code(secret, clock)

    # Act
    response = _login(client, route, totp_code=code)

    # Assert
    assert (response.status_code, response.json()) == (429, TOO_MANY_ATTEMPTS)
    assert response.headers["Retry-After"] == str(LOCKOUT_SECONDS)
    assert "access_token" not in response.text
    totp_after = _totp_rows(session_factory)
    assert totp_after == totp_before
    assert [(row["failed_attempts"], row["locked_until"]) for row in totp_after] == [(0, None)]
    assert len(_audit(session_factory, "auth.login_failed")) == totp_login_failures_before


# --- the lock is checked again after the slot, and after argon2 -------------------------------------------

PASSWORD_KINDS = [pytest.param(PASSWORD, id="correct"), pytest.param(WRONG_PASSWORD, id="wrong")]
PRIOR_FAILURES = [pytest.param(0, id="no-prior-failures"), pytest.param(3, id="three-prior-failures")]
LOCK_WAIT_SECONDS = 5.0


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize("password", PASSWORD_KINDS)
def test_a_request_waiting_for_a_slot_gets_429_when_the_account_is_locked_meanwhile_and_runs_no_argon2(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    route: str,
    password: str,
) -> None:
    # Arrange
    waiting = threading.Event()
    slots = _SignallingSlots(1, waiting)
    client = _client(session_factory, clock, private_pem, login_slots=slots)
    user_id = _sign_up(session_factory, clock)
    calls = _count_argon2_verifications(monkeypatch)
    audit_rows_before = _audit_count(session_factory)
    assert slots.acquire(blocking=False) is True
    waiting.clear()
    responses: list[Response] = []
    request = threading.Thread(target=lambda: responses.append(_login(client, route, password=password)))

    # Act
    request.start()
    assert waiting.wait(timeout=LOCK_WAIT_SECONDS) is True
    _write_lock(session_factory, clock, user_id)
    slots.release()
    request.join(timeout=LOCK_WAIT_SECONDS)

    # Assert
    assert request.is_alive() is False
    assert [(r.status_code, r.json()) for r in responses] == [(429, TOO_MANY_ATTEMPTS)]
    assert responses[0].headers["Retry-After"] == str(LOCKOUT_SECONDS)
    assert "access_token" not in responses[0].text
    assert calls == []
    assert _throttle_rows(session_factory) == [_locked_row(user_id, clock)]
    assert _audit_count(session_factory) == audit_rows_before
    assert slots.acquire(blocking=False) is True


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize("prior_failures", PRIOR_FAILURES)
def test_a_correct_password_that_finishes_argon2_after_a_lock_gets_429_and_does_not_reset_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    route: str,
    prior_failures: int,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _fail(client, route, prior_failures)
    audit_rows_before = _audit_count(session_factory)
    original_verify = PasswordHasher.verify

    def locking_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        _write_lock(session_factory, clock, user_id)
        return original_verify(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", locking_verify)

    # Act
    response = _login(client, route)

    # Assert
    assert (response.status_code, response.json()) == (429, TOO_MANY_ATTEMPTS)
    assert response.headers["Retry-After"] == str(LOCKOUT_SECONDS)
    assert "access_token" not in response.text
    assert _throttle_rows(session_factory) == [_locked_row(user_id, clock)]
    assert _audit(session_factory, "auth.login") == []
    assert _audit_count(session_factory) == audit_rows_before


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize("prior_failures", PRIOR_FAILURES)
def test_a_wrong_password_that_finishes_argon2_after_a_lock_gets_429_not_401_and_is_not_counted(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    route: str,
    prior_failures: int,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    user_id = _sign_up(session_factory, clock)
    _fail(client, route, prior_failures)
    audit_rows_before = _audit_count(session_factory)
    failed_rows_before = len(_audit(session_factory, "auth.login_failed"))
    original_verify = PasswordHasher.verify

    def locking_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        _write_lock(session_factory, clock, user_id)
        return original_verify(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", locking_verify)

    # Act
    response = _login(client, route, password=WRONG_PASSWORD)

    # Assert
    assert (response.status_code, response.json()) == (429, TOO_MANY_ATTEMPTS)
    assert response.headers["Retry-After"] == str(LOCKOUT_SECONDS)
    assert _throttle_rows(session_factory) == [_locked_row(user_id, clock)]
    assert len(_audit(session_factory, "auth.login_failed")) == failed_rows_before
    assert _audit(session_factory, "auth.login_locked") == []
    assert _audit_count(session_factory) == audit_rows_before
