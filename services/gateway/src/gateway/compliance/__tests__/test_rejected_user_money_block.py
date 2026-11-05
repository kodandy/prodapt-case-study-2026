"""RED tests for M3, from docs/specs/compliance-rbac.md section 5: a `rejected` user moves no money at all.
`screen_operation` blocks ANY operation (transfer or payout) by a `rejected` user and audits
`kyc.operation_blocked`; `ensure_not_rejected(session_factory, clock, user_id, operation)` returns True
when the user may proceed and, for a `rejected` user, audits `kyc.operation_blocked` (summary: operation,
kyc_status) and returns False."""

import uuid

import pytest
from gateway.accounts.service import sign_up
from gateway.clock import FixedClock
from gateway.compliance.kyc import AMOUNT_THRESHOLD_MINOR, OperationRequest, screen_operation
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "route-test-password-7Q"
THRESHOLD = AMOUNT_THRESHOLD_MINOR["USD"]
BLOCKED_ACTION = "kyc.operation_blocked"


# --- arrange helpers -------------------------------------------------------------------------------------


def _user(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _user_with_status(
    session_factory: sessionmaker[Session], clock: FixedClock, kyc_status: str, email: str = ALICE
) -> uuid.UUID:
    user_id = _user(session_factory, clock, email)
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.users SET kyc_status = :kyc_status WHERE id = :id"),
            {"kyc_status": kyc_status, "id": user_id},
        )
    return user_id


def _transfer_request(amount_minor: int) -> OperationRequest:
    return OperationRequest(kind="transfer", amount_minor=amount_minor, currency="USD", recipient_email=BOB)


def _payout_request(amount_minor: int) -> OperationRequest:
    return OperationRequest(kind="payout", amount_minor=amount_minor, currency="USD")


def _ensure_not_rejected(
    session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID, operation: str
) -> bool:
    # Imported here so a missing function fails these tests only, not the screen_operation tests.
    from gateway.compliance.kyc import ensure_not_rejected

    return ensure_not_rejected(session_factory, clock, user_id, operation)


# --- assert helpers --------------------------------------------------------------------------------------


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _blocked_audit(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target, summary FROM gateway.audit_log "
        "WHERE action = :action ORDER BY id",
        action=BLOCKED_ACTION,
    )


def _audit_count(session_factory: sessionmaker[Session]) -> int:
    return len(_rows(session_factory, "SELECT 1 FROM gateway.audit_log"))


def _kyc_status(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> str:
    [row] = _rows(session_factory, "SELECT kyc_status FROM gateway.users WHERE id = :id", id=user_id)
    return str(row["kyc_status"])


# --- screen_operation: a rejected user is blocked for any operation --------------------------------------


def test_a_small_domestic_transfer_by_a_rejected_user_is_blocked_without_any_risk_flag(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")
    _user(session_factory, clock, BOB)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert (
        tuple(result.triggered),
        result.cross_border,
        result.high_risk,
        result.blocked,
        result.kyc_status,
    ) == ((), False, False, True, "rejected")


def test_a_blocked_transfer_by_a_rejected_user_audits_one_operation_blocked_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")
    _user(session_factory, clock, BOB)

    # Act
    screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    [row] = _blocked_audit(session_factory)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert row["actor_id"] == f"user:{alice}"
    assert row["actor_role"] == "consumer"
    assert row["target"] == f"user:{alice}"
    assert summary["operation"] == "transfer"
    assert summary["kyc_status"] == "rejected"


def test_a_transfer_one_minor_unit_below_the_threshold_by_a_rejected_user_is_blocked_and_stays_rejected(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")
    _user(session_factory, clock, BOB)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(THRESHOLD - 1))

    # Assert
    assert (result.blocked, result.kyc_status) == (True, "rejected")
    assert _kyc_status(session_factory, alice) == "rejected"
    assert len(_blocked_audit(session_factory)) == 1


def test_a_small_payout_by_a_rejected_user_is_still_blocked_and_audited_as_a_payout(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")

    # Act
    result = screen_operation(session_factory, clock, alice, _payout_request(1_000))

    # Assert
    [row] = _blocked_audit(session_factory)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert result.blocked is True
    assert summary["operation"] == "payout"


@pytest.mark.parametrize("kyc_status", ["pending", "unverified"])
def test_a_small_domestic_transfer_by_a_pending_or_unverified_user_is_not_blocked_and_writes_no_audit(
    session_factory: sessionmaker[Session], clock: FixedClock, kyc_status: str
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, kyc_status)
    _user(session_factory, clock, BOB)

    # Act
    result = screen_operation(session_factory, clock, alice, _transfer_request(1_000))

    # Assert
    assert (result.blocked, result.high_risk, result.kyc_status) == (False, False, kyc_status)
    assert _blocked_audit(session_factory) == []


# --- ensure_not_rejected ---------------------------------------------------------------------------------


@pytest.mark.parametrize("kyc_status", ["unverified", "pending", "verified"])
def test_ensure_not_rejected_returns_true_and_writes_nothing_for_a_user_who_is_not_rejected(
    session_factory: sessionmaker[Session], clock: FixedClock, kyc_status: str
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, kyc_status)
    audit_rows_before = _audit_count(session_factory)

    # Act
    allowed = _ensure_not_rejected(session_factory, clock, alice, "payment")

    # Assert
    assert allowed is True
    assert _audit_count(session_factory) == audit_rows_before
    assert _blocked_audit(session_factory) == []
    assert _kyc_status(session_factory, alice) == kyc_status


def test_ensure_not_rejected_returns_false_for_a_rejected_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")

    # Act
    allowed = _ensure_not_rejected(session_factory, clock, alice, "payment")

    # Assert
    assert allowed is False


def test_ensure_not_rejected_audits_one_operation_blocked_row_with_the_operation_and_status(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")

    # Act
    _ensure_not_rejected(session_factory, clock, alice, "payment")

    # Assert
    [row] = _blocked_audit(session_factory)
    assert row["actor_id"] == f"user:{alice}"
    assert row["actor_role"] == "consumer"
    assert row["target"] == f"user:{alice}"
    assert row["summary"] == {"operation": "payment", "kyc_status": "rejected"}


@pytest.mark.parametrize("operation", ["payment", "transfer", "payout"])
def test_ensure_not_rejected_records_the_given_operation_in_the_audit_summary(
    session_factory: sessionmaker[Session], clock: FixedClock, operation: str
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")

    # Act
    _ensure_not_rejected(session_factory, clock, alice, operation)

    # Assert
    [row] = _blocked_audit(session_factory)
    summary = row["summary"]
    assert isinstance(summary, dict)
    assert summary["operation"] == operation


def test_each_blocked_attempt_by_a_rejected_user_writes_its_own_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")

    # Act
    results = [
        _ensure_not_rejected(session_factory, clock, alice, "payment"),
        _ensure_not_rejected(session_factory, clock, alice, "payment"),
    ]

    # Assert
    assert results == [False, False]
    assert len(_blocked_audit(session_factory)) == 2


def test_ensure_not_rejected_for_a_rejected_user_does_not_change_the_status(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")

    # Act
    _ensure_not_rejected(session_factory, clock, alice, "payment")

    # Assert
    assert _kyc_status(session_factory, alice) == "rejected"


def test_ensure_not_rejected_audits_only_the_rejected_user_not_another_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _user_with_status(session_factory, clock, "rejected")
    bob = _user_with_status(session_factory, clock, "pending", BOB)

    # Act
    results = (
        _ensure_not_rejected(session_factory, clock, bob, "payment"),
        _ensure_not_rejected(session_factory, clock, alice, "payment"),
    )

    # Assert
    [row] = _blocked_audit(session_factory)
    assert results == (True, False)
    assert row["actor_id"] == f"user:{alice}"
