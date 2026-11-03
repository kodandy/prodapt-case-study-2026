"""RED tests for B3 input hardening (red-team RT-001..004), from docs/specs/gateway-basic.md."""

import uuid
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import open_dispute
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest, DisputeView, EvidenceRequest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
INVALID_KEY_BODY = {"detail": "invalid Idempotency-Key header"}

BAD_KEYS = ["a b", "a\tb", "a\x01b", "caf\xe9", "ключ", "a\x7fb"]
BAD_KEY_IDS = ["space", "tab", "control_x01", "latin1_non_ascii", "cyrillic", "del_x7f"]
MAX_VISIBLE_ASCII_KEY = "!~" * 127 + "a"

BAD_REFUND_BODIES: list[dict[str, object]] = [
    {"amount_minor": True},
    {"amount_minor": "100"},
    {"amount_minor": 100.0},
    {"amount_minor": None},
]
BAD_REFUND_BODY_IDS = ["bool_true", "numeric_string", "float", "explicit_null"]

BAD_REASONS = ["", "   ", "a" * 65, "fraud\x00", "fraud​", "fraud‮", "a\nb"]
BAD_REASON_IDS = ["empty", "blank", "65_chars", "nul", "zero_width_space", "bidi_override", "newline"]

BAD_EVIDENCE = ["​​", "ok‮", "ok\x01", "​ \n"]
BAD_EVIDENCE_IDS = ["zero_width_only", "bidi_override", "control_x01", "zero_width_space_newline"]
MULTILINE_EVIDENCE = "line1\nline2\tend\r\n"


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    auth: dict[str, str]


def _harness(session_factory: sessionmaker[Session], clock: FixedClock) -> Harness:
    charge_deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
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


def _charge_json() -> dict[str, object]:
    return {
        "amount_minor": 1500,
        "currency": "USD",
        "method": "card",
        "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }


def _charge(h: Harness, amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate({**_charge_json(), "amount_minor": amount_minor})
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _open(h: Harness, payment_id: uuid.UUID, reason: str = "fraudulent") -> DisputeView:
    return open_dispute(h.session_factory, h.clock, payment_id, reason)


def _raw_key_headers(h: Harness, key: str) -> list[tuple[bytes, bytes]]:
    """Sends the key as raw bytes: httpx refuses non-ASCII str header values."""
    return [
        (b"authorization", h.auth["Authorization"].encode("ascii")),
        (b"idempotency-key", key.encode("utf-8")),
    ]


def _refund_url(payment_id: uuid.UUID) -> str:
    return f"/v1/payments/{payment_id}/refunds"


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    queries = {
        "payments": "SELECT count(*) AS n FROM gateway.payments",
        "refunds": "SELECT count(*) AS n FROM gateway.refunds",
        "disputes": "SELECT count(*) AS n FROM gateway.disputes",
    }
    return int(str(_rows(session_factory, queries[table])[0]["n"]))


def _totals(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> tuple[int, int, int]:
    row = _rows(
        session_factory,
        "SELECT refunded_minor, disputed_minor, chargeback_minor FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]
    return int(str(row["refunded_minor"])), int(str(row["disputed_minor"])), int(str(row["chargeback_minor"]))


def _dispute_state(session_factory: sessionmaker[Session], dispute_id: uuid.UUID) -> str:
    rows = _rows(session_factory, "SELECT state FROM gateway.disputes WHERE id = :id", id=dispute_id)
    return str(rows[0]["state"])


# --- RT-001: Idempotency-Key on POST /v1/payments ------------------------------------------------------


@pytest.mark.parametrize("key", BAD_KEYS, ids=BAD_KEY_IDS)
def test_post_payment_with_a_non_visible_ascii_idempotency_key_returns_400_and_stores_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, key: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.post("/v1/payments", json=_charge_json(), headers=_raw_key_headers(h, key))

    # Assert
    assert response.status_code == 400
    assert response.json() == INVALID_KEY_BODY
    assert _count(session_factory, "payments") == 0


def test_post_payment_with_a_255_char_visible_ascii_idempotency_key_returns_201(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.post(
        "/v1/payments", json=_charge_json(), headers={**h.auth, "Idempotency-Key": MAX_VISIBLE_ASCII_KEY}
    )

    # Assert
    assert len(MAX_VISIBLE_ASCII_KEY) == 255
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    assert _count(session_factory, "payments") == 1


def test_post_payment_without_an_idempotency_key_still_returns_the_required_message(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.post("/v1/payments", json=_charge_json(), headers=h.auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "Idempotency-Key header is required"}


# --- RT-001: Idempotency-Key on POST /v1/payments/{id}/refunds -----------------------------------------


@pytest.mark.parametrize("key", BAD_KEYS, ids=BAD_KEY_IDS)
def test_post_refund_with_a_non_visible_ascii_idempotency_key_returns_400_and_stores_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, key: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json={"amount_minor": 500}, headers=_raw_key_headers(h, key)
    )

    # Assert
    assert response.status_code == 400
    assert response.json() == INVALID_KEY_BODY
    assert _count(session_factory, "refunds") == 0
    assert _totals(session_factory, payment_id) == (0, 0, 0)


def test_post_refund_with_a_255_char_visible_ascii_idempotency_key_returns_201(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id),
        json={"amount_minor": 500},
        headers={**h.auth, "Idempotency-Key": MAX_VISIBLE_ASCII_KEY},
    )

    # Assert
    assert len(MAX_VISIBLE_ASCII_KEY) == 255
    assert response.status_code == 201
    assert response.json()["payment_refunded_minor"] == 500
    assert _count(session_factory, "refunds") == 1


# --- RT-002: strict RefundRequest.amount_minor ---------------------------------------------------------


@pytest.mark.parametrize("body", BAD_REFUND_BODIES, ids=BAD_REFUND_BODY_IDS)
def test_post_refund_with_a_non_int_amount_returns_422_and_refunds_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, body: dict[str, object]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json=body, headers={**h.auth, "Idempotency-Key": "strict-amount"}
    )

    # Assert
    assert response.status_code == 422
    assert _totals(session_factory, payment_id) == (0, 0, 0)
    assert _count(session_factory, "refunds") == 0


def test_post_refund_with_a_json_null_body_returns_422_and_refunds_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id),
        content=b"null",
        headers={**h.auth, "Idempotency-Key": "null-body", "Content-Type": "application/json"},
    )

    # Assert
    assert response.status_code == 422
    assert _totals(session_factory, payment_id) == (0, 0, 0)
    assert _count(session_factory, "refunds") == 0


@pytest.mark.parametrize("body", [None, {}], ids=["no_body", "empty_object"])
def test_post_refund_with_the_amount_omitted_still_refunds_the_full_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, body: dict[str, int] | None
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    client = _client(h)

    # Act
    response = client.post(
        _refund_url(payment_id), json=body, headers={**h.auth, "Idempotency-Key": "omitted-amount"}
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["amount_minor"] == 1500
    assert _totals(session_factory, payment_id) == (1500, 0, 0)


# --- RT-003: open_dispute reason -----------------------------------------------------------------------


@pytest.mark.parametrize("reason", BAD_REASONS, ids=BAD_REASON_IDS)
def test_open_dispute_with_an_invalid_reason_raises_and_writes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, reason: str
) -> None:
    # Arrange
    from gateway.payments.disputes import InvalidDisputeReasonError

    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)

    # Act
    with pytest.raises(InvalidDisputeReasonError) as error:
        _open(h, payment_id, reason)

    # Assert
    assert error.type is InvalidDisputeReasonError
    assert _count(session_factory, "disputes") == 0
    assert _totals(session_factory, payment_id) == (0, 0, 0)


def test_open_dispute_with_a_64_char_ascii_reason_succeeds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    reason = "a" * 64

    # Act
    view = _open(h, payment_id, reason)

    # Assert
    assert view.reason == reason
    assert view.state == "opened"
    assert _totals(session_factory, payment_id) == (0, 1500, 0)


# --- RT-004: evidence text -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", BAD_EVIDENCE, ids=BAD_EVIDENCE_IDS)
def test_evidence_request_rejects_format_and_control_characters(value: str) -> None:
    # Arrange
    text_value = value

    # Act
    with pytest.raises(ValidationError) as error:
        EvidenceRequest(text=text_value)

    # Assert
    assert error.value.error_count() == 1
    assert error.value.errors()[0]["loc"] == ("text",)


def test_evidence_request_accepts_newline_tab_and_carriage_return_inside_text() -> None:
    # Arrange
    text_value = MULTILINE_EVIDENCE

    # Act
    request = EvidenceRequest(text=text_value)

    # Assert
    assert request.text == MULTILINE_EVIDENCE


@pytest.mark.parametrize("value", BAD_EVIDENCE, ids=BAD_EVIDENCE_IDS)
def test_post_evidence_with_format_or_control_characters_returns_422_and_keeps_the_dispute_opened(
    session_factory: sessionmaker[Session], clock: FixedClock, value: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.post(f"/v1/disputes/{opened.dispute_id}/evidence", json={"text": value}, headers=h.auth)

    # Assert
    assert response.status_code == 422
    assert _dispute_state(session_factory, opened.dispute_id) == "opened"


def test_post_evidence_with_newline_tab_and_carriage_return_moves_the_dispute_to_under_review(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    opened = _open(h, _charge(h))
    client = _client(h)

    # Act
    response = client.post(
        f"/v1/disputes/{opened.dispute_id}/evidence", json={"text": MULTILINE_EVIDENCE}, headers=h.auth
    )

    # Assert
    assert response.status_code == 200
    assert response.json()["state"] == "under_review"
    assert _dispute_state(session_factory, opened.dispute_id) == "under_review"
