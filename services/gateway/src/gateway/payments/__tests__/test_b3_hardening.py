"""RED regression tests for the B3 security-audit fixes (cursor, evidence, outcome, ownership)."""

import base64
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import cast

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import (
    DisputeOutcome,
    open_dispute,
    resolve_dispute,
    submit_evidence,
)
from gateway.payments.history import InvalidCursorError, list_payments
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.refunds import RefundOutcome, refund_payment
from gateway.payments.schemas import ChargeRequest, DisputeView, EvidenceRequest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
ACTOR = "ops_lead:test"

NON_UTC_TIMESTAMPS = [
    "2026-11-04T00:00:00+23:59",
    "2026-11-04T00:00:00+05:30",
    "2026-11-04T00:00:00-23:59:59.999999",
]
NON_UTC_IDS = ["plus_23_59", "plus_05_30", "minus_23_59_59_999999"]
BAD_EVIDENCE = ["\u0000", "receipt\u0000attached", "   ", "\n\t"]
BAD_EVIDENCE_IDS = ["nul_only", "nul_inside", "spaces_only", "newline_tab_only"]
BAD_OUTCOMES = ["Lost", "WON", "", "refunded"]
BAD_OUTCOME_IDS = ["capitalised_lost", "upper_won", "empty", "unknown_word"]


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    auth: dict[str, str]


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    deps: ChargeDeps | None = None,
    name: str = "Acme",
) -> Harness:
    charge_deps = deps or ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(
        charge_deps,
        session_factory,
        clock,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
    )


def _client(h: Harness) -> TestClient:
    return TestClient(create_app(GatewayAppDeps(charge=h.deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _charge(h: Harness, amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": "USD",
            "method": "card",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _charge_series(h: Harness, count: int) -> list[uuid.UUID]:
    """Creates `count` payments one minute apart; returns ids oldest first."""
    ids: list[uuid.UUID] = []
    for index in range(count):
        ids.append(_charge(h, amount_minor=1000 + index))
        h.clock.advance(timedelta(minutes=1))
    return ids


def _cursor(timestamp: str, payment_id: uuid.UUID) -> str:
    return base64.urlsafe_b64encode(f"{timestamp}|{payment_id}".encode()).decode()


def _decode_cursor_timestamp(cursor: str) -> str:
    raw = base64.urlsafe_b64decode(cursor.encode()).decode()
    return raw.split("|")[0]


def _open(h: Harness, payment_id: uuid.UUID) -> DisputeView:
    return open_dispute(h.session_factory, h.clock, payment_id, "fraudulent")


def _under_review(h: Harness, payment_id: uuid.UUID) -> DisputeView:
    opened = _open(h, payment_id)
    return submit_evidence(h.session_factory, h.clock, h.merchant_id, opened.dispute_id, "receipt attached")


def _as_outcome(value: str) -> DisputeOutcome:
    """Feeds an out-of-contract outcome to resolve_dispute, as an untyped caller could."""
    return cast(DisputeOutcome, value)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _totals(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> tuple[int, int, int]:
    row = _rows(
        session_factory,
        "SELECT refunded_minor, disputed_minor, chargeback_minor FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]
    return int(str(row["refunded_minor"])), int(str(row["disputed_minor"])), int(str(row["chargeback_minor"]))


def _journal_count_for(session_factory: sessionmaker[Session], ref_id: uuid.UUID) -> int:
    rows = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_id = :ref_id",
        ref_id=ref_id,
    )
    return int(str(rows[0]["n"]))


def _journal_count(session_factory: sessionmaker[Session]) -> int:
    rows = _rows(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries")
    return int(str(rows[0]["n"]))


def _dispute_state(session_factory: sessionmaker[Session], dispute_id: uuid.UUID) -> str:
    rows = _rows(session_factory, "SELECT state FROM gateway.disputes WHERE id = :id", id=dispute_id)
    return str(rows[0]["state"])


# --- cursor offsets ------------------------------------------------------------------------------------


@pytest.mark.parametrize("timestamp", NON_UTC_TIMESTAMPS, ids=NON_UTC_IDS)
def test_list_payments_rejects_a_cursor_with_a_non_utc_offset(
    session_factory: sessionmaker[Session], clock: FixedClock, timestamp: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    cursor = _cursor(timestamp, uuid.uuid4())

    # Act
    with session_factory() as session, pytest.raises(InvalidCursorError) as error:
        list_payments(session, h.merchant_id, cursor=cursor)

    # Assert
    assert error.type is InvalidCursorError


@pytest.mark.parametrize("timestamp", NON_UTC_TIMESTAMPS, ids=NON_UTC_IDS)
def test_get_payments_with_a_non_utc_offset_cursor_returns_400_invalid_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock, timestamp: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"cursor": _cursor(timestamp, uuid.uuid4())}, headers=h.auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


def test_list_payments_accepts_a_cursor_with_an_explicit_utc_offset(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    cursor = _cursor("2999-01-01T00:00:00+00:00", uuid.uuid4())
    ids = _charge_series(h, 2)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, cursor=cursor)

    # Assert
    assert [item.payment_id for item in page.items] == list(reversed(ids))
    assert page.next_cursor is None


def test_get_payments_next_cursor_is_encoded_in_utc(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge_series(h, 3)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"limit": 2}, headers=h.auth)

    # Assert
    assert response.status_code == 200
    next_cursor = str(response.json()["next_cursor"])
    assert _decode_cursor_timestamp(next_cursor).endswith("+00:00")


def test_list_payments_next_cursor_is_encoded_in_utc_and_walks_to_the_last_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, 3)

    # Act
    with session_factory() as session:
        first = list_payments(session, h.merchant_id, limit=2)
        first_cursor = str(first.next_cursor)
        second = list_payments(session, h.merchant_id, limit=2, cursor=first_cursor)

    # Assert
    assert _decode_cursor_timestamp(first_cursor).endswith("+00:00")
    assert [item.payment_id for item in second.items] == [ids[0]]
    assert second.next_cursor is None


# --- evidence text -------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", BAD_EVIDENCE, ids=BAD_EVIDENCE_IDS)
def test_evidence_request_rejects_nul_and_whitespace_only_text(value: str) -> None:
    # Arrange
    text_value = value

    # Act
    with pytest.raises(ValidationError) as error:
        EvidenceRequest(text=text_value)

    # Assert
    assert error.value.error_count() == 1
    assert error.value.errors()[0]["loc"] == ("text",)


@pytest.mark.parametrize("value", BAD_EVIDENCE, ids=BAD_EVIDENCE_IDS)
def test_post_evidence_with_nul_or_whitespace_only_text_returns_422_and_keeps_the_dispute_opened(
    session_factory: sessionmaker[Session], clock: FixedClock, value: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)
    url = f"/v1/disputes/{opened.dispute_id}"

    # Act
    response = client.post(f"{url}/evidence", json={"text": value}, headers=h.auth)

    # Assert
    assert response.status_code == 422
    assert client.get(url, headers=h.auth).json()["state"] == "opened"
    assert _dispute_state(session_factory, opened.dispute_id) == "opened"


def test_post_evidence_with_ordinary_text_still_moves_the_dispute_to_under_review(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)
    url = f"/v1/disputes/{opened.dispute_id}"

    # Act
    response = client.post(f"{url}/evidence", json={"text": "tracking\nnumber 123"}, headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json()["state"] == "under_review"


# --- resolve_dispute outcome ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", BAD_OUTCOMES, ids=BAD_OUTCOME_IDS)
def test_resolve_dispute_with_an_unknown_outcome_raises_value_error_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, outcome: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    reviewed = _under_review(h, payment_id)
    totals_before = _totals(session_factory, payment_id)

    # Act
    with pytest.raises(ValueError):
        resolve_dispute(h.session_factory, h.clock, reviewed.dispute_id, _as_outcome(outcome), ACTOR)

    # Assert
    assert totals_before == (0, 1500, 0)
    assert _dispute_state(session_factory, reviewed.dispute_id) == "under_review"
    assert _journal_count_for(session_factory, reviewed.dispute_id) == 0
    assert _totals(session_factory, payment_id) == totals_before


def test_resolve_dispute_with_an_unknown_outcome_writes_no_resolved_audit_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    reviewed = _under_review(h, _charge(h))

    # Act
    with pytest.raises(ValueError):
        resolve_dispute(h.session_factory, h.clock, reviewed.dispute_id, _as_outcome("Lost"), ACTOR)

    # Assert
    rows = _rows(session_factory, "SELECT id FROM gateway.audit_log WHERE action = 'dispute.resolved'")
    assert rows == []


# --- ownership of the refunded payment -----------------------------------------------------------------


def test_refund_payment_by_another_merchant_returns_404_and_leaves_the_payment_totals_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    owner = _harness(session_factory, clock)
    intruder = _harness(session_factory, clock, deps=owner.deps, name="Intruder")
    payment_id = _charge(owner)
    totals_before = _totals(session_factory, payment_id)
    journals_before = _journal_count(session_factory)

    # Act
    outcome = refund_payment(
        intruder.session_factory, intruder.clock, intruder.merchant_id, payment_id, "refund-intruder", 100
    )

    # Assert
    assert outcome == RefundOutcome(404, {"error": "payment_not_found"})
    assert totals_before == (0, 0, 0)
    assert _totals(session_factory, payment_id) == totals_before
    assert _journal_count(session_factory) == journals_before
