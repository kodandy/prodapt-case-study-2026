"""DisputeEventV1 bounds: what the ops-ai projection cannot store is rejected by the contract (M-2, L-6)."""

import uuid
from datetime import UTC, datetime

import pytest
from contracts.events import DISPUTE_MAX_AMOUNT_MINOR, DisputeEventV1
from pydantic import ValidationError

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
STATES = {
    "opened": ("dispute.opened", None, 1),
    "under_review": ("dispute.evidence_submitted", "opened", 2),
    "won": ("dispute.resolved", "under_review", 3),
    "lost": ("dispute.resolved", "under_review", 3),
}


def _payload(to_state: str = "opened", **overrides: object) -> dict[str, object]:
    event_type, from_state, version = STATES[to_state]
    payload: dict[str, object] = {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": OCCURRED_AT.isoformat(),
        "entity_version": version,
        "dispute_id": str(uuid.uuid4()),
        "payment_id": str(uuid.uuid4()),
        "merchant_id": str(uuid.uuid4()),
        "from_state": from_state,
        "to_state": to_state,
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


def _error_locations(payload: dict[str, object]) -> list[tuple[int | str, ...]]:
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)
    return [tuple(error["loc"]) for error in exc.value.errors()]


def test_the_contract_amount_ceiling_matches_the_gateway_api_ceiling() -> None:
    # Arrange
    expected = 10**12

    # Act
    ceiling = DISPUTE_MAX_AMOUNT_MINOR

    # Assert
    assert ceiling == expected


@pytest.mark.parametrize("version", [0, 4, 2**31, 2**40])
def test_an_entity_version_outside_one_to_three_is_rejected(version: int) -> None:
    # Arrange
    payload = _payload(entity_version=version)

    # Act
    locations = _error_locations(payload)

    # Assert
    assert ("entity_version",) in locations


@pytest.mark.parametrize(
    "field_name",
    [
        "payment_amount_minor",
        "refunded_minor",
        "dispute_amount_minor",
        "chargeback_minor",
        "chargeback_fee_minor",
    ],
)
@pytest.mark.parametrize("amount", [DISPUTE_MAX_AMOUNT_MINOR + 1, 2**63, 2**70])
def test_an_amount_above_the_gateway_ceiling_is_rejected(field_name: str, amount: int) -> None:
    # Arrange
    payload = _payload(**{field_name: amount})

    # Act
    locations = _error_locations(payload)

    # Assert
    assert locations == [(field_name,)]


def test_amounts_at_the_ceiling_are_accepted() -> None:
    # Arrange
    payload = _payload(
        "lost",
        payment_amount_minor=DISPUTE_MAX_AMOUNT_MINOR,
        refunded_minor=DISPUTE_MAX_AMOUNT_MINOR,
        dispute_amount_minor=DISPUTE_MAX_AMOUNT_MINOR,
        chargeback_minor=DISPUTE_MAX_AMOUNT_MINOR,
        chargeback_fee_minor=DISPUTE_MAX_AMOUNT_MINOR,
    )

    # Act
    event = DisputeEventV1.model_validate(payload)

    # Assert
    assert (event.payment_amount_minor, event.chargeback_fee_minor) == (
        DISPUTE_MAX_AMOUNT_MINOR,
        DISPUTE_MAX_AMOUNT_MINOR,
    )


@pytest.mark.parametrize("currency", ["U\x00D", "usd", "US1", "U D", "ÜSD"])
def test_a_currency_that_is_not_three_upper_case_letters_is_rejected(currency: str) -> None:
    # Arrange
    payload = _payload(currency=currency)

    # Act
    locations = _error_locations(payload)

    # Assert
    assert locations == [("currency",)]


@pytest.mark.parametrize(
    ("to_state", "version"),
    [("opened", 2), ("opened", 3), ("under_review", 1), ("under_review", 3), ("won", 1), ("lost", 2)],
)
def test_an_entity_version_that_does_not_match_the_new_state_is_rejected(to_state: str, version: int) -> None:
    # Arrange
    payload = _payload(to_state, entity_version=version)

    # Act
    with pytest.raises(ValidationError) as exc:
        DisputeEventV1.model_validate(payload)

    # Assert
    assert f"entity_version {version} does not match to_state {to_state}" in str(exc.value)


@pytest.mark.parametrize("to_state", ["opened", "under_review", "won", "lost"])
def test_the_gateway_version_for_each_state_is_accepted(to_state: str) -> None:
    # Arrange
    payload = _payload(to_state)

    # Act
    event = DisputeEventV1.model_validate(payload)

    # Assert
    assert (event.to_state, event.entity_version) == (to_state, STATES[to_state][2])
