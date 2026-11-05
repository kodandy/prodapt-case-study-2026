"""Tests for `screen_operation`, `resolve_kyc` and their HTTP surface (POST /v1/me/transfers screening and the
ops KYC routes), written from docs/specs/compliance-rbac.md section 5 (AML / KYC re-verification)."""

import json
import uuid
from datetime import datetime, timedelta

import pyotp
import pytest
from contracts.compliance import COMPLIANCE_TOPIC, KycReviewRequiredV1, parse_compliance_event
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.admin import OpsPrincipal, OpsRole, bootstrap_ops_user
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.compliance.kyc import (
    AMOUNT_THRESHOLD_MINOR,
    KYC_MAX_AGE,
    OperationRequest,
    ScreeningResult,
    UnknownUserError,
    resolve_kyc,
    screen_operation,
)
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.service import list_wallets, top_up_wallet, transfer
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "route-test-password-7Q"
OPS_PASSWORD = "ops-correct-horse-battery-9Z"
OPS_EMAIL = "agent@ops.example.com"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
THRESHOLD = AMOUNT_THRESHOLD_MINOR["USD"]
SPEC_USD_THRESHOLD = 1_000_000
TRANSFERS = "/v1/me/transfers"
OPS_SESSIONS = "/v1/ops/sessions"
KYC_REVIEWS = "/v1/ops/kyc-reviews"
KYC_REVIEW_ITEM_KEYS = {
    "review_id",
    "user_id",
    "status",
    "reasons",
    "opened_at",
    "resolved_at",
    "resolved_by",
}
INVALID_OPS_SESSION = {"detail": "invalid ops session"}
STEP_SECONDS = 30


def _generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def private_pem() -> str:
    return _generate_private_pem()


# --- arrange helpers -------------------------------------------------------------------------------------


def _user(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    email: str,
    country: str | None = "US",
    currency: str = "USD",
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD, currency=currency)
        session.commit()
    if country is not None:
        _set_country(session_factory, result.user_id, country)
    return result.user_id


def _set_country(session_factory: sessionmaker[Session], user_id: uuid.UUID, country: str | None) -> None:
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET country = :country WHERE id = :id"),
            {"country": country, "id": user_id},
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


def _fund(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    amount_minor: int,
    currency: str = "USD",
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, currency, amount_minor)


def _send(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender: uuid.UUID,
    recipient_email: str,
    amount_minor: int,
    key: str,
    currency: str = "USD",
) -> None:
    outcome = transfer(session_factory, clock, sender, key, recipient_email, amount_minor, currency)
    assert outcome.status_code == 201


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    return len(_rows(session_factory, f"SELECT 1 FROM gateway.{table}"))  # noqa: S608 - fixed table names


def _user_row(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> dict[str, object]:
    [row] = _rows(
        session_factory,
        "SELECT kyc_status, kyc_verified_at, kyc_expires_at FROM gateway.users WHERE id = :id",
        id=user_id,
    )
    return row


def _reviews(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, user_id, status, reasons, opened_at, resolved_at, resolved_by "
        "FROM gateway.kyc_reviews ORDER BY opened_at, id",
    )


def _audit(session_factory: sessionmaker[Session], action: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log "
        "WHERE action = :action ORDER BY id",
        action=action,
    )


def _outbox(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(session_factory, "SELECT topic, payload, event_id FROM gateway.outbox ORDER BY id")


def _transfer_request(
    amount_minor: int, recipient_email: str = BOB, currency: str = "USD"
) -> OperationRequest:
    return OperationRequest(
        kind="transfer", amount_minor=amount_minor, currency=currency, recipient_email=recipient_email
    )


def _outcome(result: ScreeningResult) -> tuple[tuple[str, ...], bool, bool, bool, str]:
    return (
        tuple(result.triggered),
        result.cross_border,
        result.high_risk,
        result.blocked,
        result.kyc_status,
    )


def _pair(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender_country: str | None = "US",
    recipient_country: str | None = "US",
) -> tuple[uuid.UUID, uuid.UUID]:
    alice = _user(session_factory, clock, ALICE, sender_country)
    bob = _user(session_factory, clock, BOB, recipient_country)
    return alice, bob


def _principal(role: OpsRole = "ops_agent") -> OpsPrincipal:
    return OpsPrincipal(ops_user_id=uuid.uuid4(), role=role)


# --- screen_operation: no rule ---------------------------------------------------------------------------


def test_a_small_domestic_transfer_triggers_nothing_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")
    assert _user_row(session_factory, alice)["kyc_status"] == "unverified"
    assert (_count(session_factory, "kyc_reviews"), _count(session_factory, "audit_log")) == (0, 0)
    assert _count(session_factory, "outbox") == 0


# --- screen_operation: a rule fires ----------------------------------------------------------------------


def test_the_amount_rule_returns_a_pending_result_without_blocking_a_domestic_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))

    # Assert
    assert _outcome(result) == (("amount_threshold",), False, False, False, "pending")


def test_the_amount_rule_moves_the_user_to_pending_and_opens_one_review_with_the_rule(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)

    # Act
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))

    # Assert
    [review] = _reviews(session_factory)
    assert _user_row(session_factory, alice)["kyc_status"] == "pending"
    assert (review["user_id"], review["status"], review["reasons"]) == (alice, "open", ["amount_threshold"])
    assert (review["opened_at"], review["resolved_at"], review["resolved_by"]) == (clock(), None, None)


def test_the_amount_rule_audits_kyc_review_opened_for_the_user_without_any_email(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)

    # Act
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))

    # Assert
    [row] = _audit(session_factory, "kyc.review_opened")
    summary = json.dumps(row["summary"])
    assert row["actor_id"] == f"user:{alice}"
    assert "amount_threshold" in summary
    assert "transfer" in summary
    assert (ALICE in summary, BOB in summary) == (False, False)


def test_the_amount_rule_writes_one_compliance_outbox_event_matching_the_review(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)

    # Act
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))

    # Assert
    [review] = _reviews(session_factory)
    [outbox] = _outbox(session_factory)
    payload = outbox["payload"]
    assert isinstance(payload, dict)
    event = parse_compliance_event(payload)
    assert outbox["topic"] == COMPLIANCE_TOPIC
    assert isinstance(event, KycReviewRequiredV1)
    assert (event.review_id, event.user_id, list(event.rules)) == (review["id"], alice, ["amount_threshold"])
    assert event.event_id == outbox["event_id"]
    assert ALICE not in json.dumps(payload)


def test_a_second_firing_merges_its_rules_into_the_same_open_review(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, bob = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    _set_country(session_factory, bob, "IR")

    # Act
    second = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    [review] = _reviews(session_factory)
    assert tuple(second.triggered) == ("high_risk_country_pair",)
    assert (review["status"], review["reasons"]) == ("open", ["amount_threshold", "high_risk_country_pair"])
    assert _user_row(session_factory, alice)["kyc_status"] == "pending"


# --- screen_operation: rules fed by committed history ----------------------------------------------------


def test_the_volume_rule_counts_the_prior_24_hour_sent_volume_from_committed_transfers(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 3 * THRESHOLD)
    _send(session_factory, clock, alice, BOB, THRESHOLD + THRESHOLD // 2, "k-1")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD // 2))

    # Assert
    assert _outcome(result) == (("volume_24h",), False, False, False, "pending")


def test_the_volume_rule_does_not_fire_one_minor_unit_below_twice_the_threshold(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 3 * THRESHOLD)
    _send(session_factory, clock, alice, BOB, THRESHOLD + THRESHOLD // 2, "k-1")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD // 2 - 1))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_the_volume_rule_ignores_transfers_sent_more_than_24_hours_ago(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 3 * THRESHOLD)
    _send(session_factory, clock, alice, BOB, THRESHOLD + THRESHOLD // 2, "k-1")
    clock.advance(timedelta(hours=25))

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD // 2))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_the_volume_rule_counts_sent_volume_only_so_a_recipient_is_not_charged_for_what_it_received(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, bob = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 3 * THRESHOLD)
    _send(session_factory, clock, alice, BOB, THRESHOLD + THRESHOLD // 2, "k-1")

    # Act
    result = screen_operation(session_factory, clock, bob, _transfer_request(THRESHOLD // 2, ALICE))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_the_volume_rule_counts_only_the_requested_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 3 * THRESHOLD, "EUR")
    _send(session_factory, clock, alice, BOB, THRESHOLD + THRESHOLD // 2, "k-1", "EUR")

    # Act
    request = _transfer_request(THRESHOLD // 2, currency="USD")
    result = screen_operation(session_factory, clock, alice, request)

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_the_velocity_rule_fires_after_ten_transfers_in_the_last_hour(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 1_000)
    for n in range(10):
        _send(session_factory, clock, alice, BOB, 1, f"k-{n}")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1))

    # Assert
    assert _outcome(result) == (("velocity",), False, False, False, "pending")


def test_the_velocity_rule_does_not_fire_after_nine_transfers(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 1_000)
    for n in range(9):
        _send(session_factory, clock, alice, BOB, 1, f"k-{n}")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_the_velocity_rule_ignores_transfers_older_than_an_hour(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, 1_000)
    for n in range(10):
        _send(session_factory, clock, alice, BOB, 1, f"k-{n}")
    clock.advance(timedelta(minutes=61))

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


# --- screen_operation: cross-border and blocking ---------------------------------------------------------


def test_an_unverified_cross_border_transfer_is_high_risk_and_blocked_without_opening_a_review(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", "DE")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == ((), True, True, True, "unverified")
    assert _count(session_factory, "kyc_reviews") == 0
    assert _user_row(session_factory, alice)["kyc_status"] == "unverified"


def test_a_blocked_attempt_writes_a_kyc_operation_blocked_audit_row_for_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", "DE")

    # Act
    screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    [row] = _audit(session_factory, "kyc.operation_blocked")
    assert row["actor_id"] == f"user:{alice}"
    assert (ALICE in json.dumps(row["summary"]), BOB in json.dumps(row["summary"])) == (False, False)


def test_a_verified_sender_is_not_blocked_on_the_same_cross_border_pair(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", "DE")
    _set_verified(session_factory, clock, alice)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == ((), True, True, False, "verified")
    assert _audit(session_factory, "kyc.operation_blocked") == []


def test_a_verified_domestic_large_transfer_triggers_pending_but_is_not_blocked(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    _set_verified(session_factory, clock, alice)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))

    # Assert
    assert _outcome(result) == (("amount_threshold",), False, False, False, "pending")
    assert _user_row(session_factory, alice)["kyc_status"] == "pending"


def test_a_verified_cross_border_large_transfer_is_blocked_because_the_rule_moves_the_user_to_pending(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", "DE")
    _set_verified(session_factory, clock, alice)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))

    # Assert
    assert _outcome(result) == (("amount_threshold",), True, True, True, "pending")
    assert len(_audit(session_factory, "kyc.operation_blocked")) == 1


def test_a_transfer_with_an_unknown_country_is_not_cross_border(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", None)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


def test_a_high_risk_country_pair_rule_fires_and_blocks_an_unverified_us_to_ir_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", "IR")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == (("high_risk_country_pair",), True, True, True, "pending")


# --- screen_operation: payouts ---------------------------------------------------------------------------


def test_a_payout_at_the_amount_threshold_is_high_risk_and_blocked_for_an_unverified_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    request = OperationRequest(kind="payout", amount_minor=THRESHOLD, currency="USD")

    # Act
    result = screen_operation(session_factory, clock, alice, request)

    # Assert
    assert _outcome(result) == (("amount_threshold",), False, True, True, "pending")


def test_a_payout_one_minor_unit_below_the_threshold_is_not_high_risk(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    request = OperationRequest(kind="payout", amount_minor=THRESHOLD - 1, currency="USD")

    # Act
    result = screen_operation(session_factory, clock, alice, request)

    # Assert
    assert _outcome(result) == ((), False, False, False, "unverified")


# --- screen_operation: re-verification after expiry ------------------------------------------------------


def test_a_verification_that_has_reached_365_days_triggers_expiry_and_staleness_on_the_next_operation(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    resolve_kyc(session_factory, clock, _principal(), alice, "verified")
    clock.advance(KYC_MAX_AGE)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == (("kyc_expired", "kyc_stale"), False, False, False, "pending")


# --- resolve_kyc -----------------------------------------------------------------------------------------


def test_resolving_verified_sets_the_status_and_a_365_day_expiry_from_now(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)

    # Act
    resolve_kyc(session_factory, clock, _principal(), alice, "verified")

    # Assert
    assert _user_row(session_factory, alice) == {
        "kyc_status": "verified",
        "kyc_verified_at": clock(),
        "kyc_expires_at": clock() + timedelta(days=365),
    }


def test_resolving_verified_closes_the_open_review_for_the_ops_actor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    principal = _principal()
    clock.advance(timedelta(minutes=5))

    # Act
    resolve_kyc(session_factory, clock, principal, alice, "verified")

    # Assert
    [review] = _reviews(session_factory)
    assert (review["status"], review["resolved_by"], review["resolved_at"]) == (
        "verified",
        f"ops:{principal.ops_user_id}",
        clock(),
    )


def test_resolving_verified_audits_kyc_reverified_by_the_ops_actor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    principal = _principal("ops_lead")

    # Act
    resolve_kyc(session_factory, clock, principal, alice, "verified")

    # Assert
    [row] = _audit(session_factory, "kyc.reverified")
    assert (row["actor_id"], row["actor_role"]) == (f"ops:{principal.ops_user_id}", "ops_lead")
    assert ALICE not in json.dumps(row["summary"])


def test_resolving_rejected_sets_the_status_closes_the_review_and_audits_kyc_rejected(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    principal = _principal()

    # Act
    resolve_kyc(session_factory, clock, principal, alice, "rejected")

    # Assert
    [review] = _reviews(session_factory)
    [row] = _audit(session_factory, "kyc.rejected")
    assert _user_row(session_factory, alice)["kyc_status"] == "rejected"
    assert (review["status"], review["resolved_by"]) == ("rejected", f"ops:{principal.ops_user_id}")
    assert row["actor_id"] == f"ops:{principal.ops_user_id}"


def test_a_rejected_user_stays_blocked_on_a_cross_border_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice, _ = _pair(session_factory, clock, "US", "DE")
    resolve_kyc(session_factory, clock, _principal(), alice, "rejected")

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert _outcome(result) == ((), True, True, True, "rejected")


def test_resolving_for_an_unknown_user_raises_unknown_user_error_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    unknown = uuid.uuid4()

    # Act
    with pytest.raises(UnknownUserError):
        resolve_kyc(session_factory, clock, _principal(), unknown, "verified")

    # Assert
    assert _count(session_factory, "audit_log") == 0


def test_the_spec_threshold_for_usd_is_one_million_minor_units() -> None:
    # Arrange / Act
    threshold = AMOUNT_THRESHOLD_MINOR["USD"]

    # Assert
    assert threshold == SPEC_USD_THRESHOLD


# --- HTTP: POST /v1/me/transfers screening ---------------------------------------------------------------


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


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _session_token(private_pem: str, clock: FixedClock, user_id: uuid.UUID) -> str:
    return issue_session_token(private_pem, user_id, clock(), ("pwd",))


def _post_transfer(
    client: TestClient, token: str, amount_minor: int, recipient_email: str = BOB, key: str = "k-1"
) -> Response:
    response: Response = client.post(
        TRANSFERS,
        headers={**_bearer(token), "Idempotency-Key": key},
        json={"recipient_email": recipient_email, "amount_minor": amount_minor, "currency": "USD"},
    )
    return response


def _balances(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def test_post_transfer_by_an_unverified_sender_across_borders_is_refused_with_403_and_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _bob = _pair(session_factory, clock, "US", "DE")
    _fund(session_factory, clock, alice, 10_000)

    # Act
    response = _post_transfer(client, _session_token(private_pem, clock, alice), 2_500)

    # Assert
    assert response.status_code == 403
    assert response.json() == {"error": "kyc_reverification_required"}
    assert response.headers["Cache-Control"] == "no-store"


def test_a_refused_cross_border_transfer_moves_no_money_and_audits_the_block(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, bob = _pair(session_factory, clock, "US", "DE")
    _fund(session_factory, clock, alice, 10_000)

    # Act
    _post_transfer(client, _session_token(private_pem, clock, alice), 2_500)

    # Assert
    assert _balances(session_factory, alice) == [("USD", 10_000, 0)]
    assert _balances(session_factory, bob) == [("USD", 0, 0)]
    assert _count(session_factory, "transfers") == 0
    assert len(_audit(session_factory, "kyc.operation_blocked")) == 1


def test_post_transfer_between_users_in_the_same_country_succeeds_with_201(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, bob = _pair(session_factory, clock, "US", "US")
    _fund(session_factory, clock, alice, 10_000)

    # Act
    response = _post_transfer(client, _session_token(private_pem, clock, alice), 2_500)

    # Assert
    assert response.status_code == 201
    assert response.json()["sender_available_minor"] == 7_500
    assert _balances(session_factory, bob) == [("USD", 2_500, 0)]


def test_post_transfer_across_borders_by_a_verified_sender_succeeds_with_201(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, bob = _pair(session_factory, clock, "US", "DE")
    _set_verified(session_factory, clock, alice)
    _fund(session_factory, clock, alice, 10_000)

    # Act
    response = _post_transfer(client, _session_token(private_pem, clock, alice), 2_500)

    # Assert
    assert response.status_code == 201
    assert _balances(session_factory, bob) == [("USD", 2_500, 0)]


def test_post_domestic_transfer_at_the_threshold_succeeds_and_leaves_the_sender_pending(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    _fund(session_factory, clock, alice, THRESHOLD)

    # Act
    response = _post_transfer(client, _session_token(private_pem, clock, alice), THRESHOLD)

    # Assert
    assert response.status_code == 201
    assert _user_row(session_factory, alice)["kyc_status"] == "pending"
    assert [review["reasons"] for review in _reviews(session_factory)] == [["amount_threshold"]]


# --- HTTP: ops KYC routes --------------------------------------------------------------------------------


def _ops_login(
    client: TestClient, session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[str, uuid.UUID]:
    """Enrols an ops_agent, logs in over HTTP and returns (bearer session token, ops_user_id)."""
    enrolment = bootstrap_ops_user(session_factory, clock, OPS_EMAIL, OPS_PASSWORD, "ops_agent", TOTP_KEY)
    code = pyotp.TOTP(enrolment.totp_secret).at(int(clock().timestamp()))
    credentials = {"email": OPS_EMAIL, "password": OPS_PASSWORD, "totp_code": code}
    response = client.post(OPS_SESSIONS, json=credentials)
    assert response.status_code == 201
    return str(response.json()["access_token"]), enrolment.ops_user_id


def _parse(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def test_ops_lists_open_kyc_reviews_with_the_specified_item_shape(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    token, _ops_user_id = _ops_login(client, session_factory, clock)
    [review] = _reviews(session_factory)

    # Act
    response = client.get(KYC_REVIEWS, params={"status": "open"}, headers=_bearer(token))

    # Assert
    [item] = response.json()["items"]
    opened_at = item.pop("opened_at")
    assert response.status_code == 200
    assert set(response.json()) == {"items"}
    assert item == {
        "review_id": str(review["id"]),
        "user_id": str(alice),
        "status": "open",
        "reasons": ["amount_threshold"],
        "resolved_at": None,
        "resolved_by": None,
    }
    assert _parse(opened_at) == clock()


def test_ops_kyc_review_items_carry_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    token, _ops_user_id = _ops_login(client, session_factory, clock)

    # Act
    response = client.get(KYC_REVIEWS, params={"status": "open"}, headers=_bearer(token))

    # Assert
    assert {key for item in response.json()["items"] for key in item} == KYC_REVIEW_ITEM_KEYS


def test_ops_kyc_review_list_filters_by_status(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    token, _ops_user_id = _ops_login(client, session_factory, clock)

    # Act
    verified = client.get(KYC_REVIEWS, params={"status": "verified"}, headers=_bearer(token))

    # Assert
    assert (verified.status_code, verified.json()) == (200, {"items": []})


def test_ops_verify_route_returns_200_with_the_new_status_and_expiry(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD))
    token, ops_user_id = _ops_login(client, session_factory, clock)

    # Act
    response = client.post(
        f"/v1/ops/users/{alice}/kyc/verify", json={"outcome": "verified"}, headers=_bearer(token)
    )

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"user_id", "kyc_status", "kyc_expires_at"}
    assert (body["user_id"], body["kyc_status"]) == (str(alice), "verified")
    assert _parse(body["kyc_expires_at"]) == clock() + timedelta(days=365)
    [review] = _reviews(session_factory)
    assert (review["status"], review["resolved_by"]) == ("verified", f"ops:{ops_user_id}")
    [audit] = _audit(session_factory, "kyc.reverified")
    assert (audit["actor_id"], audit["actor_role"]) == (f"ops:{ops_user_id}", "ops_agent")


def test_ops_verify_route_with_rejected_outcome_returns_200_and_rejects_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    token, _ops_user_id = _ops_login(client, session_factory, clock)

    # Act
    response = client.post(
        f"/v1/ops/users/{alice}/kyc/verify", json={"outcome": "rejected"}, headers=_bearer(token)
    )

    # Assert
    assert response.status_code == 200
    assert (response.json()["user_id"], response.json()["kyc_status"]) == (str(alice), "rejected")
    assert _user_row(session_factory, alice)["kyc_status"] == "rejected"


def test_ops_verify_route_for_an_unknown_user_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    token, _ops_user_id = _ops_login(client, session_factory, clock)

    # Act
    response = client.post(
        f"/v1/ops/users/{uuid.uuid4()}/kyc/verify", json={"outcome": "verified"}, headers=_bearer(token)
    )

    # Assert
    assert response.status_code == 404


@pytest.mark.parametrize("body", [{"outcome": "approved"}, {"outcome": "pending"}, {}, {"outcome": None}])
def test_ops_verify_route_rejects_an_invalid_outcome_with_422_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    token, _ops_user_id = _ops_login(client, session_factory, clock)

    # Act
    response = client.post(f"/v1/ops/users/{alice}/kyc/verify", json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 422
    assert _user_row(session_factory, alice)["kyc_status"] == "unverified"


def test_ops_kyc_routes_answer_a_consumer_session_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)
    headers = _bearer(_session_token(private_pem, clock, alice))

    # Act
    listing = client.get(KYC_REVIEWS, params={"status": "open"}, headers=headers)
    verify = client.post(f"/v1/ops/users/{alice}/kyc/verify", json={"outcome": "verified"}, headers=headers)

    # Assert
    assert (listing.status_code, listing.json()) == (401, INVALID_OPS_SESSION)
    assert (verify.status_code, verify.json()) == (401, INVALID_OPS_SESSION)
    assert _user_row(session_factory, alice)["kyc_status"] == "unverified"


def test_ops_kyc_routes_answer_a_missing_token_with_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice, _ = _pair(session_factory, clock)

    # Act
    listing = client.get(KYC_REVIEWS, params={"status": "open"})
    verify = client.post(f"/v1/ops/users/{alice}/kyc/verify", json={"outcome": "verified"})

    # Assert
    assert (listing.status_code, verify.status_code) == (401, 401)
