"""Tests for merchant_uuid, seed_shift and to_payment_event, written from docs/specs/sq1.md section 4."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import PaymentEventV1
from ops_ai.seed import MERCHANT_NAMESPACE, merchant_uuid, seed_shift, to_payment_event

from scripts.generate_dataset import (
    CanonicalTransaction,
    PaymentMethod,
    PlantedScenarios,
    RecordKind,
    Status,
)

WINDOW_END = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
TX_TIME = datetime(2026, 10, 31, 14, 30, tzinfo=UTC)
SHIFT = timedelta(days=3, hours=6)
TRANSACTION_ID = uuid.UUID("0a0a0a0a-0000-4000-8000-000000000001")
FAILURE_REASON_ON_BIN = "Issuer unavailable (timeout) for BIN 400000"


def _tx(
    *,
    merchant_id: str | None = "mch_0001",
    payment_method: PaymentMethod = "card",
    status: Status = "success",
    failure_reason: str | None = None,
    timestamp: datetime = TX_TIME,
    record_kind: RecordKind = "merchant_payment",
    card_bin: str | None = "424242",
    acquirer: str | None = "acq_b",
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=TRANSACTION_ID,
        user_id=uuid.UUID("0b0b0b0b-0000-4000-8000-000000000001"),
        merchant_id=merchant_id,
        amount_minor=2599,
        currency="EUR",
        payment_method=payment_method,
        status=status,
        failure_reason=failure_reason,
        fraud_score=0.1,
        timestamp=timestamp,
        chargeback_flag=False,
        settlement_status="settled",
        resolution_notes=None,
        kyc_status="verified",
        country_sender="DE",
        country_receiver="DE",
        record_kind=record_kind,
        scenario="baseline",
        card_bin=card_bin,
        acquirer=acquirer,
        rule_id=None,
        sender_balance_minor=None,
    )


def _planted(window_end: datetime = WINDOW_END) -> PlantedScenarios:
    return PlantedScenarios(
        sq1_merchant_id="mch_0001",
        sq1_window_start=window_end - timedelta(hours=6),
        sq1_window_end=window_end,
        sq1_card_bin="400000",
        sq2_transaction_id=uuid.UUID(int=2),
        sq3_merchant_id="mch_0002",
        sq3_transaction_ids=(uuid.UUID(int=3),),
        sq4_merchant_id="mch_0003",
    )


def _expected_event(**overrides: object) -> PaymentEventV1:
    fields: dict[str, object] = {
        "event_id": uuid.uuid5(MERCHANT_NAMESPACE, f"seed:{TRANSACTION_ID}"),
        "event_type": "payment.succeeded",
        "occurred_at": TX_TIME + SHIFT,
        "origin": "backfill",
        "entity_version": 1,
        "payment_id": TRANSACTION_ID,
        "merchant_id": merchant_uuid("mch_0001"),
        "amount_minor": 2599,
        "currency": "EUR",
        "method": "card",
        "state": "succeeded",
        "provider": "acq_b",
        "decline_code": None,
        "card_bin": "424242",
        "card_last4": None,
    }
    fields.update(overrides)
    return PaymentEventV1.model_validate(fields)


# --- merchant_uuid ---------------------------------------------------------------------------------------


def test_merchant_namespace_is_a_fixed_uuid() -> None:
    # Arrange / Act
    namespace = MERCHANT_NAMESPACE

    # Assert
    assert isinstance(namespace, uuid.UUID)


def test_merchant_uuid_is_the_uuid5_of_the_external_id_in_the_namespace() -> None:
    # Arrange / Act
    result = merchant_uuid("mch_0001")

    # Assert
    assert result == uuid.uuid5(MERCHANT_NAMESPACE, "mch_0001")
    assert result.version == 5


def test_merchant_uuid_is_deterministic() -> None:
    # Arrange / Act
    first = merchant_uuid("mch_0001")
    second = merchant_uuid("mch_0001")

    # Assert
    assert first == second


def test_merchant_uuid_differs_between_external_ids() -> None:
    # Arrange / Act
    ids = {merchant_uuid(f"mch_{n:04d}") for n in range(1, 41)}

    # Assert
    assert len(ids) == 40


# --- seed_shift ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("anchor", "expected"),
    [
        (datetime(2026, 11, 3, 12, 0, tzinfo=UTC), timedelta(days=2, hours=18)),
        (WINDOW_END, timedelta(0)),
        (datetime(2026, 10, 30, 18, 0, tzinfo=UTC), timedelta(days=-1)),
        (datetime(2026, 11, 3, 12, 0, 30, tzinfo=UTC), timedelta(days=2, hours=18, seconds=30)),
    ],
    ids=["later_anchor", "anchor_equals_window_end", "earlier_anchor", "seconds_offset"],
)
def test_seed_shift_is_the_anchor_minus_the_sq1_window_end(anchor: datetime, expected: timedelta) -> None:
    # Arrange
    planted = _planted()

    # Act
    shift = seed_shift(planted, anchor)

    # Assert
    assert shift == expected


# --- to_payment_event: field mapping ---------------------------------------------------------------------


def test_to_payment_event_maps_a_successful_card_payment_field_by_field() -> None:
    # Arrange
    tx = _tx()

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event == _expected_event()


def test_to_payment_event_derives_event_id_from_the_transaction_id_and_keeps_payment_id() -> None:
    # Arrange
    tx = _tx()

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is not None
    assert event.payment_id == TRANSACTION_ID
    assert event.event_id == uuid.uuid5(MERCHANT_NAMESPACE, f"seed:{TRANSACTION_ID}")
    assert event.event_id != TRANSACTION_ID


def test_to_payment_event_marks_the_event_as_backfill_version_one_without_last4() -> None:
    # Arrange
    tx = _tx()

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is not None
    assert (event.origin, event.entity_version, event.method, event.card_last4) == (
        "backfill",
        1,
        "card",
        None,
    )


def test_to_payment_event_shifts_occurred_at_by_the_given_delta() -> None:
    # Arrange
    tx = _tx(timestamp=datetime(2026, 10, 31, 12, 0, 7, tzinfo=UTC))

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is not None
    assert event.occurred_at == datetime(2026, 11, 3, 18, 0, 7, tzinfo=UTC)


def test_to_payment_event_uses_the_acquirer_as_provider_and_the_dataset_card_bin() -> None:
    # Arrange
    tx = _tx(acquirer="acq_a", card_bin="400000")

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is not None
    assert (event.provider, event.card_bin) == ("acq_a", "400000")


def test_to_payment_event_maps_the_merchant_through_merchant_uuid() -> None:
    # Arrange
    tx = _tx(merchant_id="mch_0007")

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is not None
    assert event.merchant_id == merchant_uuid("mch_0007")


# --- to_payment_event: state map -------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["success", "reversed"])
def test_to_payment_event_maps_success_and_reversed_to_succeeded_without_a_decline_code(
    status: Status,
) -> None:
    # Arrange
    tx = _tx(status=status)

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event == _expected_event(event_type="payment.succeeded", state="succeeded", decline_code=None)


@pytest.mark.parametrize(
    ("status", "failure_reason", "decline_code"),
    [
        ("failed", FAILURE_REASON_ON_BIN, "issuer_unavailable"),
        ("failed", "Issuer declined: do_not_honor", "do_not_honor"),
        ("failed", "Insufficient funds on card", "insufficient_funds"),
        ("failed", "Card expired", "expired_card"),
        ("failed", "Incorrect CVC", "incorrect_cvc"),
        ("flagged", "Flagged by fraud model: velocity anomaly", "fraud_flagged"),
        ("failed", "Something the taxonomy does not know", "other"),
    ],
)
def test_to_payment_event_maps_failed_and_flagged_to_declined_with_the_taxonomy_code(
    status: Status, failure_reason: str, decline_code: str
) -> None:
    # Arrange
    tx = _tx(status=status, failure_reason=failure_reason)

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event == _expected_event(
        event_type="payment.declined", state="declined", decline_code=decline_code
    )


def test_to_payment_event_maps_pending_to_unknown_without_a_decline_code() -> None:
    # Arrange
    tx = _tx(status="pending")

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event == _expected_event(event_type="payment.unknown", state="unknown", decline_code=None)


# --- to_payment_event: records that are not seeded -------------------------------------------------------


def test_to_payment_event_returns_none_for_a_p2p_transfer() -> None:
    # Arrange
    tx = _tx(record_kind="p2p_transfer", merchant_id=None, payment_method="bank_transfer", card_bin=None)

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is None


@pytest.mark.parametrize("payment_method", ["bank_transfer", "wallet", "upi"])
def test_to_payment_event_returns_none_for_a_merchant_payment_that_is_not_a_card(
    payment_method: PaymentMethod,
) -> None:
    # Arrange
    tx = _tx(payment_method=payment_method, card_bin=None, acquirer=None)

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is None


def test_to_payment_event_returns_none_for_a_p2p_record_even_when_it_is_paid_by_card() -> None:
    # Arrange
    tx = _tx(record_kind="p2p_transfer", merchant_id=None, payment_method="card")

    # Act
    event = to_payment_event(tx, SHIFT)

    # Assert
    assert event is None
