"""Tests for the dispute workflow event on the ops port (contracts.events.DisputeEventV1)."""

import uuid
from datetime import UTC, datetime

import pytest
from contracts.events import DISPUTES_TOPIC, DisputeEventV1
from pydantic import ValidationError

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
EXPECTED_FIELDS = {
    "event_id",
    "event_type",
    "occurred_at",
    "origin",
    "entity_version",
    "dispute_id",
    "payment_id",
    "merchant_id",
    "from_state",
    "to_state",
    "actor_role",
    "network_reason_code",
    "currency",
    "payment_amount_minor",
    "refunded_minor",
    "dispute_amount_minor",
    "chargeback_minor",
    "chargeback_fee_minor",
}


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "dispute.opened",
        "occurred_at": OCCURRED_AT.isoformat(),
        "entity_version": 1,
        "dispute_id": str(uuid.uuid4()),
        "payment_id": str(uuid.uuid4()),
        "merchant_id": str(uuid.uuid4()),
        "from_state": None,
        "to_state": "opened",
        "actor_role": "card_network",
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 1500,
        "refunded_minor": 0,
        "dispute_amount_minor": 1500,
        "chargeback_minor": 0,
        "chargeback_fee_minor": 0,
    }
    payload.update(overrides)
    return payload


def test_the_dispute_topic_is_versioned_like_the_other_ops_port_topics() -> None:
    # Arrange
    expected = "gw.disputes.v1"

    # Act
    topic = DISPUTES_TOPIC

    # Assert
    assert topic == expected


def test_the_event_carries_only_ids_amounts_states_actor_categories_and_a_reason_code() -> None:
    # Arrange
    fields = set(DisputeEventV1.model_fields)

    # Act
    personal_or_card = {name for name in fields if any(t in name for t in ("card", "pan", "email", "user"))}

    # Assert
    assert fields == EXPECTED_FIELDS
    assert personal_or_card == set()


def test_a_valid_opened_event_round_trips_through_json() -> None:
    # Arrange
    event = DisputeEventV1.model_validate(_payload())

    # Act
    parsed = DisputeEventV1.model_validate_json(event.model_dump_json())

    # Assert
    assert parsed == event
    assert parsed.origin == "live"


@pytest.mark.parametrize(
    "field_name",
    ["evidence_text", "reason", "payer_user_id", "card_last4"],
)
def test_an_extra_field_is_rejected(field_name: str) -> None:
    # Arrange
    payload = _payload(**{field_name: "x"})

    # Act
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)

    # Assert
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


@pytest.mark.parametrize("code", ["not me", "13.1; DROP", "1", "13.1.2.3", ""])
def test_a_reason_code_that_is_not_a_network_code_is_rejected(code: str) -> None:
    # Arrange
    payload = _payload(network_reason_code=code)

    # Act
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)

    # Assert
    assert exc.value.errors()[0]["loc"] == ("network_reason_code",)


@pytest.mark.parametrize("code", ["10.4", "12.6.1", "13.1"])
def test_network_shaped_reason_codes_are_accepted(code: str) -> None:
    # Arrange
    payload = _payload(network_reason_code=code)

    # Act
    event = DisputeEventV1.model_validate(payload)

    # Assert
    assert event.network_reason_code == code


@pytest.mark.parametrize(
    ("event_type", "to_state"),
    [
        ("dispute.opened", "under_review"),
        ("dispute.evidence_submitted", "opened"),
        ("dispute.resolved", "under_review"),
    ],
)
def test_an_event_type_that_does_not_match_the_new_state_is_rejected(event_type: str, to_state: str) -> None:
    # Arrange
    payload = _payload(event_type=event_type, to_state=to_state, from_state="opened")

    # Act
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)

    # Assert
    assert "does not match" in str(exc.value)


def test_a_naive_timestamp_is_rejected() -> None:
    # Arrange
    payload = _payload(occurred_at="2026-11-03T11:00:00")

    # Act
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)

    # Assert
    assert exc.value.errors()[0]["loc"] == ("occurred_at",)


def test_an_unknown_actor_role_is_rejected() -> None:
    # Arrange
    payload = _payload(actor_role="merchant_api")

    # Act
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)

    # Assert
    assert exc.value.errors()[0]["loc"] == ("actor_role",)
