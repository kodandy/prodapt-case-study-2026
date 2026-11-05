"""HTTP tests for the AML / KYC screening of POST /v1/me/payouts, written from docs/specs/compliance-rbac.md
section 5 (a payout >= the amount threshold is high-risk; blocked = high-risk and the status after screening
is not `verified`; a blocked attempt audits `kyc.operation_blocked`). The route screens like
POST /v1/me/transfers: it holds `user_operation_lock` across screening and the payout, answers 403
`{"error": "kyc_reverification_required"}` when blocked and 409 `{"error": "operation_in_progress"}` with
`Retry-After: 1` when the per-user lock is held. Both refusals are `Cache-Control: no-store`.
"""

import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.compliance.kyc import AMOUNT_THRESHOLD_MINOR, user_operation_lock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
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
MASKED = "****3000"
THRESHOLD = AMOUNT_THRESHOLD_MINOR["USD"]
NO_STORE = "no-store"
KYC_REFUSAL = {"error": "kyc_reverification_required"}
OPERATION_IN_PROGRESS = {"error": "operation_in_progress"}
SMALL_AMOUNT = 2_500


def _generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def private_pem() -> str:
    return _generate_private_pem()


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


def _user(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _fund(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, amount_minor: int
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, "USD", amount_minor)


def _set_kyc_status(session_factory: sessionmaker[Session], user_id: uuid.UUID, kyc_status: str) -> None:
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET kyc_status = :kyc_status WHERE id = :id"),
            {"kyc_status": kyc_status, "id": user_id},
        )


def _funded_user(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    email: str = ALICE,
    kyc_status: str | None = None,
) -> uuid.UUID:
    user_id = _user(session_factory, clock, email)
    _fund(session_factory, clock, user_id, 2 * THRESHOLD)
    if kyc_status is not None:
        _set_kyc_status(session_factory, user_id, kyc_status)
    return user_id


def _token(private_pem: str, clock: FixedClock, user_id: uuid.UUID) -> str:
    return issue_session_token(private_pem, user_id, clock(), ("pwd",))


def _post_payout(client: TestClient, token: str, amount_minor: int, key: str = "k-1") -> Response:
    response: Response = client.post(
        PAYOUTS,
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": key},
        json={
            "method": "bank_transfer",
            "amount_minor": amount_minor,
            "currency": "USD",
            "destination": DESTINATION,
        },
    )
    return response


# --- assert helpers --------------------------------------------------------------------------------------


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _payout_count(session_factory: sessionmaker[Session]) -> int:
    return len(_rows(session_factory, "SELECT 1 FROM gateway.payouts"))


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _kyc_status(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> str:
    [row] = _rows(session_factory, "SELECT kyc_status FROM gateway.users WHERE id = :id", id=user_id)
    return str(row["kyc_status"])


def _blocked_audit(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, summary FROM gateway.audit_log "
        "WHERE action = 'kyc.operation_blocked' ORDER BY id",
    )


def _reviews(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory, "SELECT user_id, status, reasons FROM gateway.kyc_reviews ORDER BY opened_at, id"
    )


# --- blocked: high-risk payout by a user who is not verified ---------------------------------------------


def test_post_payout_at_the_threshold_by_an_unverified_user_is_refused_with_403_and_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), THRESHOLD)

    # Assert
    assert response.status_code == 403
    assert response.json() == KYC_REFUSAL
    assert response.headers["Cache-Control"] == NO_STORE


def test_a_refused_threshold_payout_creates_no_payout_and_holds_no_funds(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)

    # Act
    _post_payout(client, _token(private_pem, clock, alice), THRESHOLD)

    # Assert
    assert _payout_count(session_factory) == 0
    assert _wallets(session_factory, alice) == [("USD", 2 * THRESHOLD, 0)]


def test_a_refused_threshold_payout_audits_one_operation_blocked_row_for_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)

    # Act
    _post_payout(client, _token(private_pem, clock, alice), THRESHOLD)

    # Assert
    [row] = _blocked_audit(session_factory)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert row["actor_id"] == f"user:{alice}"
    assert row["action"] == "kyc.operation_blocked"
    assert summary["operation"] == "payout"


def test_a_refused_payout_far_above_the_threshold_is_refused_the_same_way(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), 2 * THRESHOLD)

    # Assert
    assert (response.status_code, response.json()) == (403, KYC_REFUSAL)
    assert _wallets(session_factory, alice) == [("USD", 2 * THRESHOLD, 0)]


@pytest.mark.parametrize("kyc_status", ["rejected", "pending"])
def test_post_payout_at_the_threshold_by_a_rejected_or_pending_user_is_refused_with_403(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, kyc_status: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, kyc_status=kyc_status)

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), THRESHOLD)

    # Assert
    assert response.status_code == 403
    assert response.json() == KYC_REFUSAL
    assert response.headers["Cache-Control"] == NO_STORE
    assert _payout_count(session_factory) == 0
    assert _wallets(session_factory, alice) == [("USD", 2 * THRESHOLD, 0)]
    assert len(_blocked_audit(session_factory)) == 1


# --- not blocked -----------------------------------------------------------------------------------------


def test_post_payout_one_minor_unit_below_the_threshold_by_an_unverified_user_is_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), THRESHOLD - 1)

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "payout_id": str(uuid.UUID(body["payout_id"])),
        "state": "succeeded",
        "method": "bank_transfer",
        "amount_minor": THRESHOLD - 1,
        "currency": "USD",
        "destination_masked": MASKED,
        "failure_code": None,
    }
    assert _payout_count(session_factory) == 1
    assert _wallets(session_factory, alice) == [("USD", THRESHOLD + 1, 0)]
    assert _blocked_audit(session_factory) == []
    assert _kyc_status(session_factory, alice) == "unverified"


# --- per-user operation lock -----------------------------------------------------------------------------


def test_post_payout_while_the_users_operation_lock_is_held_returns_409_with_retry_after(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    token = _token(private_pem, clock, alice)

    # Act
    with user_operation_lock(session_factory, alice):
        response = _post_payout(client, token, SMALL_AMOUNT)

    # Assert
    assert response.status_code == 409
    assert response.json() == OPERATION_IN_PROGRESS
    assert response.headers["Cache-Control"] == NO_STORE
    assert response.headers["Retry-After"] == "1"


def test_a_payout_refused_for_a_held_lock_creates_no_payout_and_holds_no_funds(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)
    token = _token(private_pem, clock, alice)

    # Act
    with user_operation_lock(session_factory, alice):
        _post_payout(client, token, SMALL_AMOUNT)

    # Assert
    assert _payout_count(session_factory) == 0
    assert _wallets(session_factory, alice) == [("USD", 2 * THRESHOLD, 0)]


def test_holding_another_users_operation_lock_does_not_block_this_users_payout(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock, ALICE)
    bob = _funded_user(session_factory, clock, BOB)
    token = _token(private_pem, clock, alice)

    # Act
    with user_operation_lock(session_factory, bob):
        response = _post_payout(client, token, SMALL_AMOUNT)

    # Assert
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    assert _wallets(session_factory, alice) == [("USD", 2 * THRESHOLD - SMALL_AMOUNT, 0)]
    assert _wallets(session_factory, bob) == [("USD", 2 * THRESHOLD, 0)]


# --- screening runs before the payout --------------------------------------------------------------------


def test_a_refused_threshold_payout_still_moves_the_user_to_pending_and_opens_a_review(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_user(session_factory, clock)

    # Act
    response = _post_payout(client, _token(private_pem, clock, alice), THRESHOLD)

    # Assert
    assert response.status_code == 403
    assert _kyc_status(session_factory, alice) == "pending"
    assert _reviews(session_factory) == [
        {"user_id": alice, "status": "open", "reasons": ["amount_threshold"]}
    ]
