"""R2-10: a transfer with an unknown country on either side is screened as cross-border (docs/specs/
compliance-rbac.md section 5). Unknown is not a fact of cross-border; it only means screening cannot rule it
out, so an unverified sender is blocked and audited without a new rule or review."""

import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.compliance.gdpr import erase_subject
from gateway.compliance.kyc import (
    KYC_MAX_AGE,
    OperationRequest,
    ScreeningResult,
    screen_operation,
)
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.service import top_up_wallet
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "route-test-password-7Q"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
BLOCKED = "kyc.operation_blocked"
KYC_REQUIRED_BODY = {"error": "kyc_reverification_required"}


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _user(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str, country: str | None
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD, currency="USD")
        session.commit()
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET country = :country WHERE id = :id"),
            {"country": country, "id": result.user_id},
        )
    return result.user_id


def _set_status(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, status: str
) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE gateway.users SET kyc_status = :status, kyc_verified_at = :verified_at, "
                "kyc_expires_at = :expires_at WHERE id = :id"
            ),
            {"status": status, "verified_at": clock(), "expires_at": clock() + KYC_MAX_AGE, "id": user_id},
        )


def _transfer(amount_minor: int = 1_000, recipient_email: str = BOB) -> OperationRequest:
    return OperationRequest(
        kind="transfer", amount_minor=amount_minor, currency="USD", recipient_email=recipient_email
    )


def _outcome(result: ScreeningResult) -> tuple[tuple[str, ...], bool, bool, bool, str]:
    return (tuple(result.triggered), result.cross_border, result.high_risk, result.blocked, result.kyc_status)


def _blocked_summaries(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            dict(row["summary"])
            for row in session.execute(
                text("SELECT summary FROM gateway.audit_log WHERE action = :a ORDER BY id"), {"a": BLOCKED}
            ).mappings()
        ]


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    with session_factory() as session:
        return len(session.execute(text(f"SELECT 1 FROM gateway.{table}")).all())  # noqa: S608 - fixed names


# --- screen_operation ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sender_country", "recipient_country"),
    [("US", None), (None, "US"), (None, None)],
    ids=["recipient_unknown", "sender_unknown", "both_unknown"],
)
def test_an_unverified_transfer_with_an_unknown_country_is_screened_as_cross_border_and_blocked(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender_country: str | None,
    recipient_country: str | None,
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, sender_country)
    _user(session_factory, clock, BOB, recipient_country)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer())

    # Assert
    assert _outcome(result) == ((), True, True, True, "unverified")
    assert _count(session_factory, "kyc_reviews") == 0
    assert _count(session_factory, "outbox") == 0


@pytest.mark.parametrize(
    ("sender_country", "recipient_country", "sender_unknown", "recipient_unknown"),
    [("US", None, False, True), (None, "US", True, False), (None, None, True, True)],
    ids=["recipient_unknown", "sender_unknown", "both_unknown"],
)
def test_a_block_on_an_unknown_country_audits_which_side_is_unknown(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender_country: str | None,
    recipient_country: str | None,
    sender_unknown: bool,
    recipient_unknown: bool,
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, sender_country)
    _user(session_factory, clock, BOB, recipient_country)

    # Act
    screen_operation(session_factory, clock, alice, _transfer())

    # Assert
    assert _blocked_summaries(session_factory) == [
        {
            "operation": "transfer",
            "cross_border": True,
            "large_payout": False,
            "kyc_status": "unverified",
            "sender_country_unknown": sender_unknown,
            "recipient_country_unknown": recipient_unknown,
        }
    ]


def test_a_block_on_known_different_countries_audits_both_countries_as_known(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, "US")
    _user(session_factory, clock, BOB, "DE")

    # Act
    screen_operation(session_factory, clock, alice, _transfer())

    # Assert
    [summary] = _blocked_summaries(session_factory)
    assert (summary["sender_country_unknown"], summary["recipient_country_unknown"]) == (False, False)


def test_a_verified_sender_passes_a_transfer_with_an_unknown_recipient_country(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, None)
    _user(session_factory, clock, BOB, None)
    _set_status(session_factory, clock, alice, "verified")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer())

    # Assert
    assert _outcome(result) == ((), True, True, False, "verified")
    assert _blocked_summaries(session_factory) == []


def test_a_rejected_sender_is_blocked_on_a_transfer_with_an_unknown_country(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, "US")
    _user(session_factory, clock, BOB, None)
    _set_status(session_factory, clock, alice, "rejected")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer())

    # Assert
    assert _outcome(result) == ((), True, True, True, "rejected")


def test_a_small_payout_by_a_user_without_a_country_is_unchanged_and_not_high_risk(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, None)
    request = OperationRequest(kind="payout", amount_minor=1_000, currency="USD")

    # Act
    result = screen_operation(session_factory, clock, alice, request)

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_a_transfer_to_an_email_that_is_not_a_user_is_not_screened_as_cross_border(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE, None)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer(recipient_email="nobody@example.com"))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


# --- HTTP ------------------------------------------------------------------------------------------------


def _client(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> TestClient:
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
    )
    return TestClient(create_app(deps))


def _post(
    client: TestClient, private_pem: str, clock: FixedClock, sender: uuid.UUID, recipient_email: str
) -> tuple[int, dict[str, object]]:
    token = issue_session_token(private_pem, sender, clock(), ("pwd",))
    response = client.post(
        "/v1/me/transfers",
        headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "k-1"},
        json={"recipient_email": recipient_email, "amount_minor": 1_000, "currency": "USD"},
    )
    return response.status_code, response.json()


def test_post_transfer_to_a_recipient_without_a_country_answers_the_same_403_as_any_kyc_block(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE, "US")
    _user(session_factory, clock, BOB, None)
    with session_factory.begin() as session:
        top_up_wallet(session, clock, alice, "USD", 10_000)

    # Act
    outcome = _post(client, private_pem, clock, alice, BOB)

    # Assert
    assert outcome == (403, KYC_REQUIRED_BODY)


def test_post_transfer_to_an_erased_recipient_keeps_answering_recipient_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE, "US")
    bob = _user(session_factory, clock, BOB, "US")
    erase_subject(session_factory, clock, bob)
    with session_factory.begin() as session:
        top_up_wallet(session, clock, alice, "USD", 10_000)

    # Act
    outcome = _post(client, private_pem, clock, alice, BOB)

    # Assert
    assert outcome == (404, {"error": "recipient_not_found"})
