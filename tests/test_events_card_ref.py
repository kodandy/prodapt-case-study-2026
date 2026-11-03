"""Tests for PaymentEventV1.card_ref, written from docs/specs/sq3.md section 1."""

import uuid
from datetime import UTC, datetime

import pytest
from contracts.events import PaymentEventV1
from pydantic import ValidationError

CARD_REF = "0123456789abcdef0123456789abcdef"
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": str(uuid.UUID(int=1)),
        "event_type": "payment.declined",
        "occurred_at": OCCURRED_AT.isoformat(),
        "entity_version": 1,
        "payment_id": str(uuid.UUID(int=2)),
        "merchant_id": str(uuid.UUID(int=3)),
        "amount_minor": 50,
        "currency": "USD",
        "method": "card",
        "state": "declined",
        "provider": "acq_a",
        "decline_code": "card_declined",
        "card_bin": "400000",
        "card_last4": "0002",
    }
    payload.update(overrides)
    return payload


def test_card_ref_accepts_32_lowercase_hex_characters() -> None:
    # Arrange
    payload = _payload(card_ref=CARD_REF)

    # Act
    event = PaymentEventV1.model_validate(payload)

    # Assert
    assert event.card_ref == CARD_REF


def test_card_ref_defaults_to_none_for_a_payload_without_the_field() -> None:
    # Arrange
    payload = _payload()

    # Act
    event = PaymentEventV1.model_validate(payload)

    # Assert
    assert "card_ref" not in payload
    assert event.card_ref is None


def test_card_ref_accepts_an_explicit_none() -> None:
    # Arrange
    payload = _payload(card_ref=None)

    # Act
    event = PaymentEventV1.model_validate(payload)

    # Assert
    assert event.card_ref is None


@pytest.mark.parametrize(
    "card_ref",
    [
        CARD_REF.upper(),
        CARD_REF[:31],
        CARD_REF + "0",
        "g" * 32,
        "",
        " " + CARD_REF[:31],
        CARD_REF + "\n",
    ],
    ids=["uppercase", "31_chars", "33_chars", "non_hex", "empty", "leading_space", "trailing_newline"],
)
def test_card_ref_rejects_anything_but_32_lowercase_hex_characters(card_ref: str) -> None:
    # Arrange
    payload = _payload(card_ref=card_ref)

    # Act / Assert
    with pytest.raises(ValidationError):
        PaymentEventV1.model_validate(payload)


def test_card_ref_rejects_a_non_string_value() -> None:
    # Arrange
    payload = _payload(card_ref=12345678901234567890123456789012)

    # Act / Assert
    with pytest.raises(ValidationError):
        PaymentEventV1.model_validate(payload)


def test_card_ref_survives_a_json_round_trip_and_none_stays_none() -> None:
    # Arrange
    with_ref = PaymentEventV1.model_validate(_payload(card_ref=CARD_REF))
    without_ref = PaymentEventV1.model_validate(_payload())

    # Act
    restored_with = PaymentEventV1.model_validate_json(with_ref.model_dump_json())
    restored_without = PaymentEventV1.model_validate_json(without_ref.model_dump_json())

    # Assert
    assert restored_with == with_ref
    assert restored_with.card_ref == CARD_REF
    assert restored_without.card_ref is None


def test_the_event_still_forbids_unknown_fields_next_to_card_ref() -> None:
    # Arrange
    payload = _payload(card_ref=CARD_REF, pan="4242424242424242")

    # Act / Assert
    with pytest.raises(ValidationError):
        PaymentEventV1.model_validate(payload)
