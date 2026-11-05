"""AML hardening of payouts against structuring, written from docs/specs/compliance-rbac.md section 5.

A user who is not `verified` must not get past the payout block by splitting a large payout into pieces below
the threshold: a payout is high-risk when its amount plus the user's pending, unknown and succeeded payouts in
the same currency over the last 24 h reaches the amount threshold (failed payouts, other currencies, other
users and payouts older than 24 h do not count). A `rejected` user is refused every payout. Prior non-failed
payouts also feed the `volume_24h` rule (sent volume) and the `velocity` rule (sent operations).
"""

import uuid
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.compliance.kyc import (
    AMOUNT_THRESHOLD_MINOR,
    KYC_MAX_AGE,
    VELOCITY_MAX_TRANSFERS,
    VOLUME_WINDOW,
    OperationRequest,
    screen_operation,
)
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.rails import SimulatedRail
from gateway.wallets.payouts import PayoutDeps, create_payout
from gateway.wallets.service import list_wallets, top_up_wallet
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

PAYOUTS = "/v1/me/payouts"
ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "route-test-password-7Q"
DESTINATION = "DE89370400440532013000"
FAIL_DESTINATION = "acct-fail-0042"
UNKNOWN_DESTINATION = "acct-unknown-0042"
THRESHOLD = AMOUNT_THRESHOLD_MINOR["USD"]
EUR_THRESHOLD = AMOUNT_THRESHOLD_MINOR["EUR"]
FUNDS = 4 * THRESHOLD
NO_STORE = "no-store"
KYC_REFUSAL = {"error": "kyc_reverification_required"}


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


# --- arrange helpers -------------------------------------------------------------------------------------


def _client(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> TestClient:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(charge=charge, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=private_pem)
    return TestClient(create_app(deps))


def _funded_user(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    email: str = ALICE,
    kyc_status: str | None = None,
    currencies: tuple[str, ...] = ("USD",),
) -> uuid.UUID:
    with session_factory() as session:
        user_id = sign_up(session, clock, email, PASSWORD).user_id
        session.commit()
    with session_factory.begin() as session:
        for currency in currencies:
            top_up_wallet(session, clock, user_id, currency, FUNDS)
    if kyc_status == "verified":
        _set_verified(session_factory, clock, user_id)
    elif kyc_status is not None:
        _set_kyc_status(session_factory, user_id, kyc_status)
    return user_id


def _set_kyc_status(session_factory: sessionmaker[Session], user_id: uuid.UUID, kyc_status: str) -> None:
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET kyc_status = :kyc_status WHERE id = :id"),
            {"kyc_status": kyc_status, "id": user_id},
        )


def _set_verified(session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE gateway.users SET kyc_status = 'verified', kyc_verified_at = :verified_at, "
                "kyc_expires_at = :expires_at WHERE id = :id"
            ),
            {"verified_at": clock(), "expires_at": clock() + KYC_MAX_AGE, "id": user_id},
        )


def _token(private_pem: str, clock: FixedClock, user_id: uuid.UUID) -> str:
    return issue_session_token(private_pem, user_id, clock(), ("pwd",))


def _post_payout(
    client: TestClient,
    token: str,
    amount_minor: int,
    key: str,
    currency: str = "USD",
    destination: str = DESTINATION,
) -> Response:
    response: Response = client.post(
        PAYOUTS,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json={
            "method": "bank_transfer",
            "amount_minor": amount_minor,
            "currency": currency,
            "destination": destination,
        },
    )
    return response


def _payout_deps(session_factory: sessionmaker[Session], clock: FixedClock) -> PayoutDeps:
    return PayoutDeps(session_factory=session_factory, clock=clock, rail=SimulatedRail())


def _prior_payout(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    amount_minor: int,
    key: str,
    destination: str = DESTINATION,
) -> None:
    """A payout made directly through the service (unscreened), so history exists whatever the user's KYC."""
    create_payout(
        _payout_deps(session_factory, clock), user_id, key, "bank_transfer", amount_minor, "USD", destination
    )


# --- assert helpers --------------------------------------------------------------------------------------


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _payout_count(session_factory: sessionmaker[Session]) -> int:
    return len(_rows(session_factory, "SELECT 1 FROM gateway.payouts"))


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _blocked_audit(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, action, summary FROM gateway.audit_log "
        "WHERE action = 'kyc.operation_blocked' ORDER BY id",
    )


def _payout_request(amount_minor: int) -> OperationRequest:
    return OperationRequest(kind="payout", amount_minor=amount_minor, currency="USD")


# --- cumulative check ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first_destination", "first_status", "first_state"),
    [(DESTINATION, 201, "succeeded"), (UNKNOWN_DESTINATION, 202, "unknown")],
)
def test_a_second_payout_that_brings_the_24_hour_total_to_the_threshold_is_refused_with_403(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    first_destination: str,
    first_status: int,
    first_state: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    token = _token(private_pem, clock, alice)
    first = _post_payout(client, token, THRESHOLD - 1, "k-1", destination=first_destination)

    # Act
    second = _post_payout(client, token, 1, "k-2")

    # Assert
    assert (first.status_code, first.json()["state"]) == (first_status, first_state)
    assert second.status_code == 403
    assert second.json() == KYC_REFUSAL
    assert second.headers["Cache-Control"] == NO_STORE


def test_a_payout_refused_for_the_cumulative_total_creates_no_payout_holds_no_funds_and_audits_the_block(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    token = _token(private_pem, clock, alice)
    _post_payout(client, token, THRESHOLD - 1, "k-1")

    # Act
    _post_payout(client, token, 1, "k-2")

    # Assert
    [row] = _blocked_audit(session_factory)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert row["actor_id"] == f"user:{alice}"
    assert summary["operation"] == "payout"
    assert _payout_count(session_factory) == 1
    assert _wallets(session_factory, alice) == [("USD", FUNDS - (THRESHOLD - 1), 0)]


def test_payouts_that_stay_below_the_threshold_in_total_are_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    token = _token(private_pem, clock, alice)
    first = _post_payout(client, token, THRESHOLD // 2, "k-1")

    # Act
    second = _post_payout(client, token, THRESHOLD // 2 - 1, "k-2")

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert _blocked_audit(session_factory) == []
    assert _payout_count(session_factory) == 2


def test_a_payout_made_within_the_last_24_hours_still_counts(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    _post_payout(client, _token(private_pem, clock, alice), THRESHOLD - 1, "k-1")
    clock.advance(timedelta(hours=23))

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), 1, "k-2")

    # Assert
    assert (response.status_code, response.json()) == (403, KYC_REFUSAL)


def test_a_payout_older_than_24_hours_does_not_count(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    _post_payout(client, _token(private_pem, clock, alice), THRESHOLD - 1, "k-1")
    clock.advance(VOLUME_WINDOW + timedelta(seconds=1))

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), 1, "k-2")

    # Assert
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    assert _blocked_audit(session_factory) == []


def test_a_failed_payout_does_not_count(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    token = _token(private_pem, clock, alice)
    first = _post_payout(client, token, THRESHOLD - 1, "k-1", destination=FAIL_DESTINATION)

    # Act
    second = _post_payout(client, token, 1, "k-2")

    # Assert
    assert (first.status_code, first.json()["state"]) == (201, "failed")
    assert second.status_code == 201
    assert second.json()["state"] == "succeeded"
    assert _blocked_audit(session_factory) == []


def test_a_payout_in_another_currency_does_not_count(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, currencies=("USD", "EUR"))
    token = _token(private_pem, clock, alice)
    first = _post_payout(client, token, EUR_THRESHOLD - 1, "k-1", currency="EUR")

    # Act
    second = _post_payout(client, token, 1, "k-2", currency="USD")

    # Assert
    assert (first.status_code, first.json()["state"]) == (201, "succeeded")
    assert second.status_code == 201
    assert second.json()["state"] == "succeeded"
    assert _blocked_audit(session_factory) == []


def test_another_users_payouts_do_not_count(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, ALICE)
    bob = _funded_user(session_factory, clock, BOB)
    _prior_payout(session_factory, clock, bob, THRESHOLD - 1, "bob-1")

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), 1, "k-1")

    # Assert
    assert response.status_code == 201
    assert _blocked_audit(session_factory) == []


def test_a_verified_user_is_not_blocked_by_the_cumulative_total(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    token = _token(private_pem, clock, alice)
    first = _post_payout(client, token, THRESHOLD - 1, "k-1")

    # Act
    second = _post_payout(client, token, 1, "k-2")

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert _blocked_audit(session_factory) == []


# --- rejected user ---------------------------------------------------------------------------------------


def test_a_rejected_user_is_refused_even_a_one_minor_unit_payout_with_403(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, kyc_status="rejected")

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), 1, "k-1")

    # Assert
    assert response.status_code == 403
    assert response.json() == KYC_REFUSAL
    assert response.headers["Cache-Control"] == NO_STORE


def test_a_rejected_users_refused_payout_creates_no_payout_holds_no_funds_and_audits_the_block(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, kyc_status="rejected")

    # Act
    _post_payout(client, _token(private_pem, clock, alice), 1, "k-1")

    # Assert
    [row] = _blocked_audit(session_factory)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert row["actor_id"] == f"user:{alice}"
    assert summary["operation"] == "payout"
    assert _payout_count(session_factory) == 0
    assert _wallets(session_factory, alice) == [("USD", FUNDS, 0)]


# --- screen_operation: prior payouts feed volume_24h and velocity ----------------------------------------


def test_prior_non_failed_payouts_plus_the_new_amount_reaching_twice_the_threshold_fire_volume_24h(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    _prior_payout(session_factory, clock, alice, THRESHOLD, "p-1")
    _prior_payout(session_factory, clock, alice, THRESHOLD // 2, "p-2", destination=UNKNOWN_DESTINATION)

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(THRESHOLD // 2))

    # Assert
    assert tuple(result.triggered) == ("volume_24h",)
    assert (result.blocked, result.kyc_status) == (True, "pending")


def test_prior_payouts_one_minor_unit_short_of_twice_the_threshold_do_not_fire_volume_24h(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    _prior_payout(session_factory, clock, alice, THRESHOLD, "p-1")
    _prior_payout(session_factory, clock, alice, THRESHOLD // 2 - 1, "p-2")

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(THRESHOLD // 2))

    # Assert
    assert tuple(result.triggered) == ()
    assert (result.blocked, result.kyc_status) == (False, "verified")


def test_a_failed_prior_payout_does_not_count_towards_volume_24h(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    _prior_payout(session_factory, clock, alice, THRESHOLD, "p-1")
    _prior_payout(session_factory, clock, alice, THRESHOLD, "p-2", destination=FAIL_DESTINATION)

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(THRESHOLD // 2))

    # Assert
    assert tuple(result.triggered) == ()
    assert result.kyc_status == "verified"


def test_a_prior_payout_older_than_24_hours_does_not_count_towards_volume_24h(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    _prior_payout(session_factory, clock, alice, THRESHOLD, "p-1")
    _prior_payout(session_factory, clock, alice, THRESHOLD // 2, "p-2")
    clock.advance(VOLUME_WINDOW + timedelta(seconds=1))

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(THRESHOLD // 2))

    # Assert
    assert tuple(result.triggered) == ()


def test_the_velocity_limit_of_prior_non_failed_payouts_in_the_last_hour_fires_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    for index in range(VELOCITY_MAX_TRANSFERS):
        _prior_payout(session_factory, clock, alice, 1, f"p-{index}")

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(1))

    # Assert
    assert tuple(result.triggered) == ("velocity",)
    assert (result.high_risk, result.blocked, result.kyc_status) == (False, False, "pending")


def test_one_prior_payout_short_of_the_velocity_limit_does_not_fire_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    for index in range(VELOCITY_MAX_TRANSFERS - 1):
        _prior_payout(session_factory, clock, alice, 1, f"p-{index}")

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(1))

    # Assert
    assert tuple(result.triggered) == ()
    assert result.kyc_status == "verified"


def test_failed_prior_payouts_do_not_count_towards_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    for index in range(VELOCITY_MAX_TRANSFERS - 1):
        _prior_payout(session_factory, clock, alice, 1, f"p-{index}")
    _prior_payout(session_factory, clock, alice, 1, "p-failed", destination=FAIL_DESTINATION)

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(1))

    # Assert
    assert tuple(result.triggered) == ()
    assert result.kyc_status == "verified"


def test_payouts_older_than_an_hour_do_not_count_towards_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _funded_user(session_factory, clock, kyc_status="verified")
    for index in range(VELOCITY_MAX_TRANSFERS):
        _prior_payout(session_factory, clock, alice, 1, f"p-{index}")
    clock.advance(timedelta(hours=1, seconds=1))

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(1))

    # Assert
    assert tuple(result.triggered) == ()
