"""Tests for NOTIFICATIONS_TOPIC and NotificationEventV1, written from docs/specs/gateway-basic.md (B11)."""

import uuid
from datetime import UTC, datetime

import pytest
from contracts.events import NOTIFICATIONS_TOPIC, PAYMENTS_TOPIC, NotificationEventV1
from pydantic import ValidationError

OCCURRED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
EVENT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
NOTIFICATION_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
SOURCE_EVENT_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
RECIPIENT_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
PAYMENT_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
EXPECTED_FIELDS = {
    "event_id",
    "event_type",
    "occurred_at",
    "origin",
    "notification_id",
    "kind",
    "source_event_id",
    "recipient_type",
    "recipient_id",
    "payment_id",
    "amount_minor",
    "currency",
}


def _fields(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event_id": EVENT_ID,
        "event_type": "notification.created",
        "occurred_at": OCCURRED_AT,
        "notification_id": NOTIFICATION_ID,
        "kind": "payment_confirmation",
        "source_event_id": SOURCE_EVENT_ID,
        "recipient_type": "merchant",
        "recipient_id": RECIPIENT_ID,
        "payment_id": PAYMENT_ID,
        "amount_minor": 1500,
        "currency": "USD",
    }
    return {**base, **overrides}


def _without(field: str) -> dict[str, object]:
    return {key: value for key, value in _fields().items() if key != field}


def test_notifications_topic_is_the_versioned_stream_name_and_differs_from_the_payments_topic() -> None:
    # Arrange: constants only

    # Act
    topics = (NOTIFICATIONS_TOPIC, PAYMENTS_TOPIC)

    # Assert
    assert topics == ("gw.notifications.v1", "gw.payments.v1")


def test_a_valid_event_keeps_every_field_and_defaults_origin_to_live() -> None:
    # Arrange
    fields = _fields()

    # Act
    event = NotificationEventV1.model_validate(fields)

    # Assert
    assert event.event_id == EVENT_ID
    assert event.event_type == "notification.created"
    assert event.occurred_at == OCCURRED_AT
    assert event.origin == "live"
    assert event.notification_id == NOTIFICATION_ID
    assert event.kind == "payment_confirmation"
    assert event.source_event_id == SOURCE_EVENT_ID
    assert event.recipient_type == "merchant"
    assert event.recipient_id == RECIPIENT_ID
    assert event.payment_id == PAYMENT_ID
    assert event.amount_minor == 1500
    assert event.currency == "USD"


def test_the_model_declares_exactly_the_pseudonymous_fields() -> None:
    # Arrange: the model class itself

    # Act
    declared = set(NotificationEventV1.model_fields)

    # Assert
    assert declared == EXPECTED_FIELDS


def test_an_event_survives_a_json_round_trip_unchanged() -> None:
    # Arrange
    event = NotificationEventV1.model_validate(_fields(kind="suspicious_activity", origin="backfill"))

    # Act
    restored = NotificationEventV1.model_validate_json(event.model_dump_json())

    # Assert
    assert restored == event


def test_the_json_dump_uses_string_ids_and_the_literal_values() -> None:
    # Arrange
    event = NotificationEventV1.model_validate(_fields())

    # Act
    dumped = event.model_dump(mode="json")

    # Assert
    assert dumped == {
        "event_id": str(EVENT_ID),
        "event_type": "notification.created",
        "occurred_at": "2026-11-03T12:00:00Z",
        "origin": "live",
        "notification_id": str(NOTIFICATION_ID),
        "kind": "payment_confirmation",
        "source_event_id": str(SOURCE_EVENT_ID),
        "recipient_type": "merchant",
        "recipient_id": str(RECIPIENT_ID),
        "payment_id": str(PAYMENT_ID),
        "amount_minor": 1500,
        "currency": "USD",
    }


def test_the_event_is_frozen() -> None:
    # Arrange
    event = NotificationEventV1.model_validate(_fields())

    # Act
    with pytest.raises(ValidationError) as raised:
        event.__setattr__("amount_minor", 1)

    # Assert
    assert raised.value.errors()[0]["type"] == "frozen_instance"
    assert event.amount_minor == 1500


@pytest.mark.parametrize("extra", ["email", "card_bin", "card_last4", "card_number", "recipient_email"])
def test_an_unknown_field_is_rejected_so_no_pan_bin_last4_or_email_can_ride_along(extra: str) -> None:
    # Arrange
    fields = _fields(**{extra: "424242"})

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [error["type"] for error in raised.value.errors()] == ["extra_forbidden"]
    assert [error["loc"] for error in raised.value.errors()] == [(extra,)]


@pytest.mark.parametrize("field", sorted(EXPECTED_FIELDS - {"origin"}))
def test_every_field_except_origin_is_required(field: str) -> None:
    # Arrange
    fields = _without(field)

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [("missing", (field,))]


@pytest.mark.parametrize("kind", ["payment_confirmation", "suspicious_activity"])
def test_both_notification_kinds_are_accepted(kind: str) -> None:
    # Arrange
    fields = _fields(kind=kind)

    # Act
    event = NotificationEventV1.model_validate(fields)

    # Assert
    assert event.kind == kind


@pytest.mark.parametrize("kind", ["transfer_received", "Payment_Confirmation", ""])
def test_an_unknown_kind_is_rejected(kind: str) -> None:
    # Arrange
    fields = _fields(kind=kind)

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [
        ("literal_error", ("kind",))
    ]


@pytest.mark.parametrize("event_type", ["notification.updated", "payment.succeeded", ""])
def test_only_notification_created_is_a_valid_event_type(event_type: str) -> None:
    # Arrange
    fields = _fields(event_type=event_type)

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [
        ("literal_error", ("event_type",))
    ]


@pytest.mark.parametrize("recipient_type", ["user", "payer", ""])
def test_only_a_merchant_is_a_valid_recipient_type(recipient_type: str) -> None:
    # Arrange
    fields = _fields(recipient_type=recipient_type)

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [
        ("literal_error", ("recipient_type",))
    ]


@pytest.mark.parametrize("origin", ["live", "backfill"])
def test_both_event_origins_are_accepted(origin: str) -> None:
    # Arrange
    fields = _fields(origin=origin)

    # Act
    event = NotificationEventV1.model_validate(fields)

    # Assert
    assert event.origin == origin


def test_an_unknown_origin_is_rejected() -> None:
    # Arrange
    fields = _fields(origin="replay")

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [
        ("literal_error", ("origin",))
    ]


def test_zero_is_a_valid_amount() -> None:
    # Arrange
    fields = _fields(amount_minor=0)

    # Act
    event = NotificationEventV1.model_validate(fields)

    # Assert
    assert event.amount_minor == 0


def test_a_negative_amount_is_rejected() -> None:
    # Arrange
    fields = _fields(amount_minor=-1)

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [
        ("greater_than_equal", ("amount_minor",))
    ]


@pytest.mark.parametrize("currency", ["US", "USDX", ""])
def test_a_currency_that_is_not_three_characters_is_rejected(currency: str) -> None:
    # Arrange
    fields = _fields(currency=currency)

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [error["loc"] for error in raised.value.errors()] == [("currency",)]


ID_FIELDS = ["event_id", "notification_id", "source_event_id", "recipient_id", "payment_id"]


@pytest.mark.parametrize("field", ID_FIELDS)
def test_an_id_that_is_not_a_uuid_is_rejected(field: str) -> None:
    # Arrange
    fields = _fields(**{field: "not-a-uuid"})

    # Act
    with pytest.raises(ValidationError) as raised:
        NotificationEventV1.model_validate(fields)

    # Assert
    assert [error["loc"] for error in raised.value.errors()] == [(field,)]
