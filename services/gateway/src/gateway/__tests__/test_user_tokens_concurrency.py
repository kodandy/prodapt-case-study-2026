"""Tests for POST /v1/user-tokens under load, written from docs/specs/sq2.md section 9.2 (audit M1)."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway import app as gateway_app
from gateway.accounts.service import sign_up
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

ROUTE = "/v1/user-tokens"
EMAIL = "sq2-sender@example.com"
PASSWORD = "route-test-password-7Q"
TRY_AGAIN_LATER = {"detail": "try again later"}


def _private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _app(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
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
    if login_slots is None:
        deps = GatewayAppDeps(
            charge=charge, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=_private_key_pem()
        )
    else:
        deps = GatewayAppDeps(
            charge=charge,
            api_key_pepper=TEST_API_KEY_PEPPER,
            jwt_private_key_pem=_private_key_pem(),
            login_slots=login_slots,
        )
    return TestClient(create_app(deps))


def _sign_up(session_factory: sessionmaker[Session], clock: FixedClock) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, EMAIL, PASSWORD)
        session.commit()
    return result.user_id


def _drained(slots: int) -> threading.BoundedSemaphore:
    semaphore = threading.BoundedSemaphore(slots)
    for _ in range(slots):
        assert semaphore.acquire(blocking=False) is True
    return semaphore


@pytest.fixture
def argon2_verify_count(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = PasswordHasher.verify

    def counting(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        calls.append("verify")
        return original(self, hash, password)

    monkeypatch.setattr(PasswordHasher, "verify", counting)
    return calls


# --- the constants ---------------------------------------------------------------------------------------


def test_login_concurrency_is_4_and_the_slot_wait_is_2_seconds() -> None:
    # Arrange / Act
    constants = (gateway_app.LOGIN_CONCURRENCY, gateway_app.LOGIN_SLOT_WAIT_SECONDS)

    # Assert
    assert constants == (4, 2.0)


# --- the session is closed before argon2 runs ------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "password", "expected_status"),
    [(EMAIL, PASSWORD, 201), (EMAIL, "wrong-password-xyz", 401), ("nobody@example.com", PASSWORD, 401)],
    ids=["correct_login", "wrong_password", "unknown_email"],
)
def test_the_route_closes_the_session_before_it_runs_the_argon2_verification(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    email: str,
    password: str,
    expected_status: int,
) -> None:
    # Arrange
    client = _app(session_factory, clock)
    _sign_up(session_factory, clock)
    events: list[str] = []
    original_verify = PasswordHasher.verify
    original_close = Session.close

    def recording_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        events.append("argon2_verify")
        return original_verify(self, hash, password)

    def recording_close(self: Session) -> None:
        events.append("session_close")
        original_close(self)

    monkeypatch.setattr(PasswordHasher, "verify", recording_verify)
    monkeypatch.setattr(Session, "close", recording_close)

    # Act
    response = client.post(ROUTE, json={"email": email, "password": password})

    # Assert
    assert response.status_code == expected_status
    assert events == ["session_close", "argon2_verify"]


# --- drained slots: 503, no check, no token --------------------------------------------------------------


def test_a_request_that_cannot_get_a_slot_gets_503_try_again_later_with_retry_after_and_no_token(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _app(session_factory, clock, login_slots=_drained(2))
    _sign_up(session_factory, clock)

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD})

    # Assert
    assert response.status_code == 503
    assert response.json() == TRY_AGAIN_LATER
    assert response.headers["Retry-After"] == "1"
    assert "access_token" not in response.text


def test_a_request_without_a_slot_runs_no_password_check(
    session_factory: sessionmaker[Session], clock: FixedClock, argon2_verify_count: list[str]
) -> None:
    # Arrange
    client = _app(session_factory, clock, login_slots=_drained(1))
    _sign_up(session_factory, clock)
    argon2_verify_count.clear()

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD})

    # Assert
    assert response.status_code == 503
    assert argon2_verify_count == []


def test_a_request_waits_about_the_slot_wait_before_it_gives_up(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _app(session_factory, clock, login_slots=_drained(1))
    _sign_up(session_factory, clock)
    started = time.monotonic()

    # Act
    response = client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD})
    waited = time.monotonic() - started

    # Assert
    assert response.status_code == 503
    assert 1.9 <= waited < 5.0


def test_a_request_for_an_unknown_email_also_gets_503_when_no_slot_is_free(
    session_factory: sessionmaker[Session], clock: FixedClock, argon2_verify_count: list[str]
) -> None:
    # Arrange
    client = _app(session_factory, clock, login_slots=_drained(1))
    argon2_verify_count.clear()

    # Act
    response = client.post(ROUTE, json={"email": "nobody@example.com", "password": PASSWORD})

    # Assert
    assert response.status_code == 503
    assert response.json() == TRY_AGAIN_LATER
    assert argon2_verify_count == []


# --- slots are released ----------------------------------------------------------------------------------


def test_a_single_slot_is_released_after_each_request_so_sequential_logins_all_succeed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _app(session_factory, clock, login_slots=threading.BoundedSemaphore(1))
    _sign_up(session_factory, clock)

    # Act
    statuses = [
        client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD}).status_code,
        client.post(ROUTE, json={"email": EMAIL, "password": "wrong-password-xyz"}).status_code,
        client.post(ROUTE, json={"email": "nobody@example.com", "password": PASSWORD}).status_code,
        client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD}).status_code,
    ]

    # Assert
    assert statuses == [201, 401, 401, 201]


# --- at most LOGIN_CONCURRENCY verifications at once -----------------------------------------------------


def test_at_most_four_argon2_verifications_run_at_once_with_the_default_slots(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    app_client = _app(session_factory, clock)
    _sign_up(session_factory, clock)
    lock = threading.Lock()
    running = 0
    peak = 0
    original_verify = PasswordHasher.verify

    def slow_verify(self: PasswordHasher, hash: str | bytes, password: str | bytes) -> bool:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        try:
            time.sleep(0.3)
            return original_verify(self, hash, password)
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(PasswordHasher, "verify", slow_verify)
    requests = 8

    def login(_: int) -> int:
        return int(app_client.post(ROUTE, json={"email": EMAIL, "password": PASSWORD}).status_code)

    # Act
    with ThreadPoolExecutor(max_workers=requests) as pool:
        statuses = list(pool.map(login, range(requests)))

    # Assert
    assert statuses == [201] * requests
    assert peak == 4
