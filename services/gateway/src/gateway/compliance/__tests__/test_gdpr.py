"""Tests for the GDPR data-subject requests (profile rectification, access export, erasure) and their HTTP
surface, written from docs/specs/compliance-rbac.md section 6 and the Events section."""

import json
import re
import uuid
from datetime import datetime, timedelta

import pyotp
import pytest
from contracts.compliance import (
    COMPLIANCE_TOPIC,
    KycReviewRequiredV1,
    SubjectErasedV1,
    parse_compliance_event,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up, verify_password
from gateway.accounts.sessions import issue_session_token
from gateway.admin import bootstrap_ops_user
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.compliance.gdpr import DSR_DEADLINE, erase_subject, export_subject_data, rectify_profile
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.service import top_up_wallet, transfer
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

PROFILE = "/v1/me/profile"
EXPORT = "/v1/me/data-requests/export"
ERASURE = "/v1/me/data-requests/erasure"
DATA_REQUESTS = "/v1/me/data-requests"
OPS_DATA_REQUESTS = "/v1/ops/data-requests"
OPS_SESSIONS = "/v1/ops/sessions"
SESSIONS = "/v1/sessions"
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
PASSWORD = "route-test-password-7Q"
WRONG_PASSWORD = "route-test-password-wrong-1A"
OPS_PASSWORD = "ops-correct-horse-battery-9Z"
OPS_EMAIL = "agent@ops.example.com"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
FULL_NAME = "Alice Example"
CHANGED_NAME = "Alice Exemplar"
ERASED_EMAIL = re.compile(r"erased-[0-9a-f]+@erased\.invalid")
PROFILE_KEYS = {"user_id", "email", "full_name", "country", "kyc_status", "kyc_expires_at"}
EXPORT_SECTIONS = {"profile", "wallets", "transfers", "kyc_reviews", "data_requests"}
INVALID_OPS_SESSION = {"detail": "invalid ops session"}
DEADLINE = timedelta(days=30)


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
    deps = GatewayAppDeps(
        charge=charge,
        api_key_pepper=TEST_API_KEY_PEPPER,
        jwt_private_key_pem=private_pem,
        totp_encryption_key=TOTP_KEY,
    )
    return TestClient(create_app(deps))


def _user(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(private_pem: str, clock: FixedClock, user_id: uuid.UUID) -> str:
    return issue_session_token(private_pem, user_id, clock(), ("pwd",))


def _auth(private_pem: str, clock: FixedClock, user_id: uuid.UUID) -> dict[str, str]:
    return _bearer(_token(private_pem, clock, user_id))


def _fund(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, amount_minor: int
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, "USD", amount_minor)


def _send(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender: uuid.UUID,
    recipient_email: str,
    amount_minor: int,
    key: str,
) -> uuid.UUID:
    outcome = transfer(session_factory, clock, sender, key, recipient_email, amount_minor, "USD")
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["transfer_id"]))


def _set_profile(
    session_factory: sessionmaker[Session], user_id: uuid.UUID, full_name: str | None, country: str | None
) -> None:
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET full_name = :full_name, country = :country WHERE id = :id"),
            {"full_name": full_name, "country": country, "id": user_id},
        )


def _set_verified(session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE gateway.users SET kyc_status = 'verified', kyc_verified_at = :verified_at, "
                "kyc_expires_at = :expires_at WHERE id = :id"
            ),
            {"verified_at": clock(), "expires_at": clock() + timedelta(days=365), "id": user_id},
        )


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _user_row(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> dict[str, object]:
    [row] = _rows(
        session_factory,
        "SELECT email, password_hash, full_name, country, kyc_status, erased_at "
        "FROM gateway.users WHERE id = :id",
        id=user_id,
    )
    return row


def _dsrs(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, user_id, kind, status, received_at, due_at, completed_at, detail "
        "FROM gateway.data_subject_requests ORDER BY received_at, id",
    )


def _audit(session_factory: sessionmaker[Session], action: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log "
        "WHERE action = :action ORDER BY id",
        action=action,
    )


def _all_audit(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, actor_id, actor_role, action, target, summary, correlation_id, occurred_at "
        "FROM gateway.audit_log ORDER BY id",
    )


def _compliance_events(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT payload FROM gateway.outbox WHERE topic = :topic ORDER BY id",
        topic=COMPLIANCE_TOPIC,
    )


def _reviews(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, user_id, status, reasons FROM gateway.kyc_reviews ORDER BY opened_at, id",
    )


def _count(session_factory: sessionmaker[Session], table: str, user_id: uuid.UUID) -> int:
    return len(_rows(session_factory, f"SELECT 1 FROM gateway.{table} WHERE user_id = :id", id=user_id))  # noqa: S608 - fixed table names


def _parse(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _patch(client: TestClient, headers: dict[str, str], body: dict[str, object]) -> Response:
    response: Response = client.patch(PROFILE, json=body, headers=headers)
    return response


# --- GET /v1/me/profile ----------------------------------------------------------------------------------


def test_get_profile_returns_the_specified_fields_for_a_new_user(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)

    # Act
    response = client.get(PROFILE, headers=_auth(private_pem, clock, alice))

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(alice),
        "email": ALICE,
        "full_name": None,
        "country": None,
        "kyc_status": "unverified",
        "kyc_expires_at": None,
    }


def test_get_profile_reflects_the_stored_name_country_and_kyc_expiry(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _set_profile(session_factory, alice, FULL_NAME, "DE")
    _set_verified(session_factory, clock, alice)

    # Act
    response = client.get(PROFILE, headers=_auth(private_pem, clock, alice))

    # Assert
    body = response.json()
    assert set(body) == PROFILE_KEYS
    assert (body["full_name"], body["country"], body["kyc_status"]) == (FULL_NAME, "DE", "verified")
    assert _parse(body["kyc_expires_at"]) == clock() + timedelta(days=365)


def test_get_profile_without_a_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)

    # Act
    response = client.get(PROFILE)

    # Assert
    assert response.status_code == 401


# --- PATCH /v1/me/profile: validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"email": "mallory@example.com"},
        {"full_name": FULL_NAME, "kyc_status": "verified"},
        {"country": "us"},
        {"country": "USA"},
        {"country": "U"},
        {"country": "1A"},
        {"full_name": ""},
        {"full_name": "a" * 201},
    ],
    ids=[
        "extra_email",
        "extra_kyc_status",
        "lowercase_country",
        "three_letter_country",
        "one_letter_country",
        "digit_country",
        "empty_name",
        "name_over_200",
    ],
)
def test_patch_profile_rejects_invalid_input_with_422_and_records_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), body)

    # Assert
    assert response.status_code == 422
    assert _dsrs(session_factory) == []
    assert _user_row(session_factory, alice)["full_name"] is None


def test_patch_profile_accepts_a_name_of_exactly_200_characters(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    name = "a" * 200

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"full_name": name})

    # Assert
    assert response.status_code == 200
    assert response.json()["full_name"] == name


# --- PATCH /v1/me/profile: rectification ----------------------------------------------------------------


def test_patch_profile_updates_the_fields_and_returns_the_profile(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"full_name": FULL_NAME, "country": "DE"})

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "user_id": str(alice),
        "email": ALICE,
        "full_name": FULL_NAME,
        "country": "DE",
        "kyc_status": "unverified",
        "kyc_expires_at": None,
    }
    stored = _user_row(session_factory, alice)
    assert (stored["full_name"], stored["country"]) == (FULL_NAME, "DE")


def test_patch_profile_records_a_completed_rectification_request_with_a_30_day_deadline(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)

    # Act
    _patch(client, _auth(private_pem, clock, alice), {"full_name": FULL_NAME, "country": "DE"})

    # Assert
    [request] = _dsrs(session_factory)
    detail = request["detail"]
    assert isinstance(detail, dict)
    assert (request["user_id"], request["kind"], request["status"]) == (alice, "rectification", "completed")
    assert request["received_at"] == clock()
    assert request["due_at"] == clock() + DEADLINE
    assert request["completed_at"] == clock()
    assert sorted(detail["fields"]) == ["country", "full_name"]
    assert FULL_NAME not in json.dumps(detail)


def test_patch_profile_with_only_a_country_records_just_that_field(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _set_profile(session_factory, alice, FULL_NAME, "DE")

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"country": "FR"})

    # Assert
    [request] = _dsrs(session_factory)
    detail = request["detail"]
    assert isinstance(detail, dict)
    assert (response.json()["full_name"], response.json()["country"]) == (FULL_NAME, "FR")
    assert detail["fields"] == ["country"]


def test_patch_profile_audits_gdpr_rectified_with_field_names_and_never_the_values(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)

    # Act
    _patch(client, _auth(private_pem, clock, alice), {"full_name": FULL_NAME, "country": "DE"})

    # Assert
    [row] = _audit(session_factory, "gdpr.rectified")
    summary = json.dumps(row["summary"])
    assert row["actor_id"] == f"user:{alice}"
    assert ("full_name" in summary, "country" in summary) == (True, True)
    assert (FULL_NAME in summary, '"DE"' in summary, ALICE in summary) == (False, False, False)


# --- PATCH /v1/me/profile: name_change -------------------------------------------------------------------


def test_setting_a_name_for_the_first_time_does_not_trigger_a_kyc_review(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"full_name": FULL_NAME})

    # Assert
    assert response.json()["kyc_status"] == "unverified"
    assert _reviews(session_factory) == []
    assert _compliance_events(session_factory) == []


def test_changing_an_existing_name_moves_the_user_to_pending_with_a_name_change_review(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _set_profile(session_factory, alice, FULL_NAME, "DE")
    _set_verified(session_factory, clock, alice)

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"full_name": CHANGED_NAME})

    # Assert
    [review] = _reviews(session_factory)
    assert response.status_code == 200
    assert response.json()["kyc_status"] == "pending"
    assert (review["user_id"], review["status"], review["reasons"]) == (alice, "open", ["name_change"])
    assert _user_row(session_factory, alice)["kyc_status"] == "pending"


def test_changing_an_existing_name_publishes_a_kyc_review_required_event_without_the_name(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _set_profile(session_factory, alice, FULL_NAME, "DE")

    # Act
    _patch(client, _auth(private_pem, clock, alice), {"full_name": CHANGED_NAME})

    # Assert
    [review] = _reviews(session_factory)
    [outbox] = _compliance_events(session_factory)
    payload = outbox["payload"]
    assert isinstance(payload, dict)
    event = parse_compliance_event(payload)
    assert isinstance(event, KycReviewRequiredV1)
    assert (event.review_id, event.user_id, list(event.rules)) == (review["id"], alice, ["name_change"])
    assert (FULL_NAME in json.dumps(payload), CHANGED_NAME in json.dumps(payload)) == (False, False)


def test_patching_the_same_name_again_does_not_trigger_a_kyc_review(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _set_profile(session_factory, alice, FULL_NAME, "DE")

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"full_name": FULL_NAME})

    # Assert
    assert response.json()["kyc_status"] == "unverified"
    assert _reviews(session_factory) == []


def test_changing_only_the_country_does_not_trigger_a_kyc_review(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _set_profile(session_factory, alice, FULL_NAME, "DE")
    _set_verified(session_factory, clock, alice)

    # Act
    response = _patch(client, _auth(private_pem, clock, alice), {"country": "FR"})

    # Assert
    assert response.json()["kyc_status"] == "verified"
    assert _reviews(session_factory) == []


# --- POST /v1/me/data-requests/export --------------------------------------------------------------------


def _export_scenario(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Alice sends 2_500 to Bob and receives 1_000; Carol and Bob trade 500 on their own.

    Returns (alice, bob, carol, carol_to_bob_transfer_id).
    """
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    carol = _user(session_factory, clock, CAROL)
    _set_profile(session_factory, alice, FULL_NAME, "US")
    _fund(session_factory, clock, alice, 10_000)
    _fund(session_factory, clock, bob, 10_000)
    _fund(session_factory, clock, carol, 10_000)
    _send(session_factory, clock, alice, BOB, 2_500, "k-a-b")
    _send(session_factory, clock, bob, ALICE, 1_000, "k-b-a")
    other = _send(session_factory, clock, carol, BOB, 500, "k-c-b")
    return alice, bob, carol, other


def test_export_returns_200_no_store_and_the_specified_envelope(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, _, _ = _export_scenario(session_factory, clock)

    # Act
    response = client.post(EXPORT, headers=_auth(private_pem, clock, alice))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(body) == {"request_id", "generated_at", "data"}
    assert set(body["data"]) == EXPORT_SECTIONS
    assert _parse(body["generated_at"]) == clock()


def test_export_profile_is_the_requesters_own(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, _, _ = _export_scenario(session_factory, clock)

    # Act
    response = client.post(EXPORT, headers=_auth(private_pem, clock, alice))

    # Assert
    profile = response.json()["data"]["profile"]
    assert (profile["email"], profile["full_name"], profile["country"]) == (ALICE, FULL_NAME, "US")


def test_export_transfers_carry_direction_and_amount_but_no_email_key(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, _, _ = _export_scenario(session_factory, clock)

    # Act
    response = client.post(EXPORT, headers=_auth(private_pem, clock, alice))

    # Assert
    transfers = response.json()["data"]["transfers"]
    keys = {key for item in transfers for key in item}
    assert sorted((item["direction"], item["amount_minor"], item["currency"]) for item in transfers) == [
        ("received", 1_000, "USD"),
        ("sent", 2_500, "USD"),
    ]
    assert [key for key in keys if "email" in key] == []
    assert {_parse(item["created_at"]) for item in transfers} == {clock()}


def test_export_contains_no_other_users_data(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, bob, carol, others_transfer = _export_scenario(session_factory, clock)

    # Act
    response = client.post(EXPORT, headers=_auth(private_pem, clock, alice))

    # Assert
    leaked = [
        needle
        for needle in (BOB, CAROL, str(bob), str(carol), str(others_transfer))
        if needle in response.text
    ]
    assert leaked == []


def test_export_lists_the_wallet_and_the_users_kyc_reviews(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, _, _ = _export_scenario(session_factory, clock)
    headers = _auth(private_pem, clock, alice)
    _patch(client, headers, {"full_name": CHANGED_NAME})

    # Act
    response = client.post(EXPORT, headers=headers)

    # Assert
    data = response.json()["data"]
    assert [wallet["currency"] for wallet in data["wallets"]] == ["USD"]
    assert [review["reasons"] for review in data["kyc_reviews"]] == [["name_change"]]


def test_export_records_a_completed_access_request_and_audits_gdpr_exported(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, _, _ = _export_scenario(session_factory, clock)

    # Act
    response = client.post(EXPORT, headers=_auth(private_pem, clock, alice))

    # Assert
    [request] = _dsrs(session_factory)
    [row] = _audit(session_factory, "gdpr.exported")
    assert str(request["id"]) == response.json()["request_id"]
    assert (request["user_id"], request["kind"], request["status"]) == (alice, "access", "completed")
    assert (request["received_at"], request["due_at"], request["completed_at"]) == (
        clock(),
        clock() + DEADLINE,
        clock(),
    )
    assert row["actor_id"] == f"user:{alice}"


def test_export_without_a_session_returns_401_and_records_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)

    # Act
    response = client.post(EXPORT)

    # Assert
    assert response.status_code == 401
    assert _dsrs(session_factory) == []


# --- POST /v1/me/data-requests/erasure -------------------------------------------------------------------


_LEDGER_TABLES = ("transfers", "journal_entries", "postings", "ledger_accounts", "account_balances")


def _ledger_snapshot(session_factory: sessionmaker[Session]) -> dict[str, list[dict[str, object]]]:
    return {
        table: _rows(session_factory, f"SELECT * FROM gateway.{table} ORDER BY 1")  # noqa: S608 - fixed table names
        for table in _LEDGER_TABLES
    }


def _erasure_scenario(
    client: TestClient,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
) -> tuple[uuid.UUID, uuid.UUID, dict[str, str]]:
    """Alice (verified, named, with an unconfirmed TOTP enrolment and a login-throttle row) paid Bob once."""
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    _set_profile(session_factory, alice, FULL_NAME, "US")
    _set_verified(session_factory, clock, alice)
    _fund(session_factory, clock, alice, 10_000)
    _send(session_factory, clock, alice, BOB, 2_500, "k-a-b")
    headers = _auth(private_pem, clock, alice)
    assert client.post("/v1/me/mfa/totp", headers=headers).status_code == 201
    failed = client.post(SESSIONS, json={"email": ALICE, "password": WRONG_PASSWORD})
    assert failed.status_code == 401
    assert _count(session_factory, "user_totp", alice) == 1
    assert _count(session_factory, "login_throttle", alice) == 1
    return alice, bob, headers


def test_erasure_returns_202_with_the_request_id_and_completed_status(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)

    # Act
    response = client.post(ERASURE, headers=headers)

    # Assert
    [request] = [row for row in _dsrs(session_factory) if row["kind"] == "erasure"]
    assert response.status_code == 202
    assert response.json() == {"request_id": str(request["id"]), "status": "completed"}
    assert (request["user_id"], request["status"]) == (alice, "completed")
    assert (request["received_at"], request["due_at"], request["completed_at"]) == (
        clock(),
        clock() + DEADLINE,
        clock(),
    )


def test_erasure_replaces_the_email_clears_name_and_country_and_stamps_erased_at(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)

    # Act
    client.post(ERASURE, headers=headers)

    # Assert
    user = _user_row(session_factory, alice)
    assert ERASED_EMAIL.fullmatch(str(user["email"])) is not None
    assert (user["full_name"], user["country"], user["erased_at"]) == (None, None, clock())


def test_erasure_replaces_the_password_hash_with_one_no_password_verifies(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)
    before = str(_user_row(session_factory, alice)["password_hash"])

    # Act
    client.post(ERASURE, headers=headers)

    # Assert
    after = str(_user_row(session_factory, alice)["password_hash"])
    assert after != before
    assert verify_password(after, PASSWORD) is False


def test_erasure_deletes_the_totp_secret_and_the_login_throttle_rows(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)

    # Act
    client.post(ERASURE, headers=headers)

    # Assert
    totp_rows = _count(session_factory, "user_totp", alice)
    throttle_rows = _count(session_factory, "login_throttle", alice)
    assert (totp_rows, throttle_rows) == (0, 0)


def test_erasure_keeps_ledger_transfers_balances_and_the_kyc_status(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)
    before = _ledger_snapshot(session_factory)

    # Act
    client.post(ERASURE, headers=headers)

    # Assert
    assert _ledger_snapshot(session_factory) == before
    assert len(before["transfers"]) == 1
    assert _user_row(session_factory, alice)["kyc_status"] == "verified"


def test_erasure_appends_exactly_one_gdpr_erased_audit_row_and_changes_no_earlier_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)
    before = _all_audit(session_factory)

    # Act
    client.post(ERASURE, headers=headers)

    # Assert
    after = _all_audit(session_factory)
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1
    assert (after[-1]["action"], after[-1]["actor_id"]) == ("gdpr.erased", f"user:{alice}")
    assert ALICE not in json.dumps(after[-1]["summary"])


def test_erasure_publishes_a_subject_erased_event_with_pseudonymous_ids_only(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)

    # Act
    response = client.post(ERASURE, headers=headers)

    # Assert
    [outbox] = _compliance_events(session_factory)
    payload = outbox["payload"]
    assert isinstance(payload, dict)
    event = parse_compliance_event(payload)
    assert isinstance(event, SubjectErasedV1)
    assert (str(event.request_id), event.user_id) == (response.json()["request_id"], alice)
    assert (ALICE in json.dumps(payload), FULL_NAME in json.dumps(payload)) == (False, False)


def test_the_old_session_is_refused_with_401_after_erasure(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)
    client.post(ERASURE, headers=headers)

    # Act
    wallets = client.get("/v1/me/wallets", headers=headers)
    profile = client.get(PROFILE, headers=headers)

    # Assert
    assert (wallets.status_code, profile.status_code) == (401, 401)


def test_login_with_the_old_credentials_is_refused_after_erasure(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, _, headers = _erasure_scenario(client, session_factory, clock, private_pem)
    client.post(ERASURE, headers=headers)

    # Act
    response = client.post(SESSIONS, json={"email": ALICE, "password": PASSWORD})

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid credentials"}


def test_a_transfer_to_the_old_email_of_an_erased_user_finds_no_recipient(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, bob, alice_headers = _erasure_scenario(client, session_factory, clock, private_pem)
    client.post(ERASURE, headers=alice_headers)
    _fund(session_factory, clock, bob, 10_000)

    # Act
    response = client.post(
        "/v1/me/transfers",
        headers={**_auth(private_pem, clock, bob), "Idempotency-Key": "k-b-a"},
        json={"recipient_email": ALICE, "amount_minor": 100, "currency": "USD"},
    )

    # Assert
    assert response.status_code == 404
    assert response.json() == {"error": "recipient_not_found"}


def test_two_erasures_get_distinct_random_placeholder_emails(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)

    # Act
    responses = [client.post(ERASURE, headers=_auth(private_pem, clock, user)) for user in (alice, bob)]

    # Assert
    emails = [str(_user_row(session_factory, user)["email"]) for user in (alice, bob)]
    assert [response.status_code for response in responses] == [202, 202]
    assert [ERASED_EMAIL.fullmatch(email) is not None for email in emails] == [True, True]
    assert emails[0] != emails[1]


def test_erasure_leaves_other_users_untouched(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _, bob, headers = _erasure_scenario(client, session_factory, clock, private_pem)
    _set_profile(session_factory, bob, "Bob Example", "DE")

    # Act
    client.post(ERASURE, headers=headers)

    # Assert
    user = _user_row(session_factory, bob)
    assert (user["email"], user["full_name"], user["country"], user["erased_at"]) == (
        BOB,
        "Bob Example",
        "DE",
        None,
    )


# --- GET /v1/me/data-requests ----------------------------------------------------------------------------


def test_listing_data_requests_returns_only_the_callers_own_with_their_deadlines(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    bob = _user(session_factory, clock, BOB)
    alice_headers = _auth(private_pem, clock, alice)
    export = client.post(EXPORT, headers=alice_headers)
    _patch(client, alice_headers, {"country": "DE"})
    _patch(client, _auth(private_pem, clock, bob), {"country": "FR"})
    [bobs] = [row for row in _dsrs(session_factory) if row["user_id"] == bob]

    # Act
    response = client.get(DATA_REQUESTS, headers=alice_headers)

    # Assert
    items = response.json()["items"]
    assert response.status_code == 200
    assert sorted((item["kind"], item["status"]) for item in items) == [
        ("access", "completed"),
        ("rectification", "completed"),
    ]
    assert str(export.json()["request_id"]) in {item["request_id"] for item in items}
    assert {_parse(item["due_at"]) for item in items} == {clock() + DEADLINE}
    assert str(bobs["id"]) not in response.text


def test_listing_data_requests_without_a_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)

    # Act
    response = client.get(DATA_REQUESTS)

    # Assert
    assert response.status_code == 401


# --- GET /v1/ops/data-requests?overdue=true --------------------------------------------------------------


def _insert_request(
    session_factory: sessionmaker[Session],
    user_id: uuid.UUID,
    kind: str,
    status: str,
    received_at: datetime,
    due_at: datetime,
    completed_at: datetime | None = None,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO gateway.data_subject_requests "
                "(id, user_id, kind, status, received_at, due_at, completed_at, detail) "
                "VALUES (:id, :user_id, :kind, :status, :received_at, :due_at, :completed_at, "
                "CAST('{}' AS jsonb))"
            ),
            {
                "id": request_id,
                "user_id": user_id,
                "kind": kind,
                "status": status,
                "received_at": received_at,
                "due_at": due_at,
                "completed_at": completed_at,
            },
        )
    return request_id


def _ops_token(client: TestClient, session_factory: sessionmaker[Session], clock: FixedClock) -> str:
    enrolment = bootstrap_ops_user(session_factory, clock, OPS_EMAIL, OPS_PASSWORD, "ops_agent", TOTP_KEY)
    code = pyotp.TOTP(enrolment.totp_secret).at(int(clock().timestamp()))
    credentials = {"email": OPS_EMAIL, "password": OPS_PASSWORD, "totp_code": code}
    response = client.post(OPS_SESSIONS, json=credentials)
    assert response.status_code == 201
    return str(response.json()["access_token"])


def test_ops_overdue_filter_lists_only_received_requests_past_their_deadline(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    long_ago = clock() - timedelta(days=40)
    overdue = _insert_request(
        session_factory, alice, "erasure", "received", long_ago, clock() - timedelta(seconds=1)
    )
    _insert_request(session_factory, alice, "access", "received", long_ago, clock())
    _insert_request(session_factory, alice, "access", "received", clock(), clock() + DEADLINE)
    _insert_request(session_factory, alice, "access", "completed", long_ago, long_ago + DEADLINE, long_ago)
    _insert_request(session_factory, alice, "erasure", "rejected", long_ago, long_ago + DEADLINE)
    token = _ops_token(client, session_factory, clock)

    # Act
    response = client.get(OPS_DATA_REQUESTS, params={"overdue": "true"}, headers=_bearer(token))

    # Assert
    [item] = response.json()["items"]
    assert response.status_code == 200
    assert {key: item[key] for key in ("request_id", "user_id", "kind", "status", "completed_at")} == {
        "request_id": str(overdue),
        "user_id": str(alice),
        "kind": "erasure",
        "status": "received",
        "completed_at": None,
    }
    assert _parse(item["received_at"]) == long_ago
    assert _parse(item["due_at"]) == clock() - timedelta(seconds=1)


def test_ops_overdue_filter_is_empty_when_every_deadline_is_still_open(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    _insert_request(session_factory, alice, "access", "received", clock(), clock() + DEADLINE)
    token = _ops_token(client, session_factory, clock)

    # Act
    response = client.get(OPS_DATA_REQUESTS, params={"overdue": "true"}, headers=_bearer(token))

    # Assert
    assert (response.status_code, response.json()) == (200, {"items": []})


def test_ops_data_requests_refuse_a_consumer_session_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _user(session_factory, clock, ALICE)
    headers = _auth(private_pem, clock, alice)

    # Act
    response = client.get(OPS_DATA_REQUESTS, params={"overdue": "true"}, headers=headers)

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_OPS_SESSION)


# --- service functions -----------------------------------------------------------------------------------


def test_the_data_subject_request_deadline_is_30_days() -> None:
    # Arrange / Act
    deadline = DSR_DEADLINE

    # Assert
    assert deadline == timedelta(days=30)


def test_rectify_profile_updates_the_user_and_records_a_completed_rectification(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)

    # Act
    rectify_profile(session_factory, clock, alice, FULL_NAME, "DE")

    # Assert
    [request] = _dsrs(session_factory)
    stored = _user_row(session_factory, alice)
    assert (stored["full_name"], stored["country"]) == (FULL_NAME, "DE")
    assert (request["kind"], request["status"], request["due_at"]) == (
        "rectification",
        "completed",
        clock() + DEADLINE,
    )


def test_export_subject_data_returns_the_five_sections_and_records_an_access_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)

    # Act
    exported = export_subject_data(session_factory, clock, alice)

    # Assert
    [request] = _dsrs(session_factory)
    data = exported["data"]
    assert isinstance(data, dict)
    assert set(data) == EXPORT_SECTIONS
    assert data["profile"]["email"] == ALICE
    assert (request["kind"], request["status"]) == ("access", "completed")


def test_erase_subject_returns_the_erasure_request_id_and_erases_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user(session_factory, clock, ALICE)

    # Act
    request_id = erase_subject(session_factory, clock, alice)

    # Assert
    [request] = _dsrs(session_factory)
    user = _user_row(session_factory, alice)
    assert request["id"] == request_id
    assert (request["kind"], request["status"]) == ("erasure", "completed")
    assert (ERASED_EMAIL.fullmatch(str(user["email"])) is not None, user["erased_at"]) == (True, clock())
