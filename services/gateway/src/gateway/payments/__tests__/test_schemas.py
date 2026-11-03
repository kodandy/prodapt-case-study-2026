import pytest
from gateway.payments.schemas import ChargeRequest
from gateway.payments.tokenization.cards import TEST_PANS
from pydantic import ValidationError

LUHN_VALID_NOT_ALLOWLISTED = "4111111111111111"
LUHN_INVALID = "4242424242424241"


def _payload(pan: str = "4242424242424242", amount_minor: int = 1500) -> dict[str, object]:
    return {
        "amount_minor": amount_minor,
        "currency": "USD",
        "method": "card",
        "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
    }


def _error_locations(exc: pytest.ExceptionInfo[ValidationError]) -> list[tuple[int | str, ...]]:
    return [error["loc"] for error in exc.value.errors()]


@pytest.mark.parametrize("pan", sorted(TEST_PANS))
def test_charge_request_accepts_every_allowlisted_test_pan(pan: str) -> None:
    # Arrange
    payload = _payload(pan=pan)

    # Act
    request = ChargeRequest.model_validate(payload)

    # Assert
    assert request.card.number.get_secret_value() == pan
    assert request.amount_minor == 1500
    assert request.currency == "USD"


def test_charge_request_rejects_a_non_luhn_number() -> None:
    # Arrange
    payload = _payload(pan=LUHN_INVALID)

    # Act
    with pytest.raises(ValidationError) as exc:
        ChargeRequest.model_validate(payload)

    # Assert
    assert ("card", "number") in _error_locations(exc)


def test_charge_request_rejects_a_luhn_valid_pan_that_is_not_allowlisted() -> None:
    # Arrange
    payload = _payload(pan=LUHN_VALID_NOT_ALLOWLISTED)

    # Act
    with pytest.raises(ValidationError) as exc:
        ChargeRequest.model_validate(payload)

    # Assert
    assert ("card", "number") in _error_locations(exc)


@pytest.mark.parametrize("amount_minor", [0, -1, -1500])
def test_charge_request_rejects_a_non_positive_amount(amount_minor: int) -> None:
    # Arrange
    payload = _payload(amount_minor=amount_minor)

    # Act
    with pytest.raises(ValidationError) as exc:
        ChargeRequest.model_validate(payload)

    # Assert
    assert _error_locations(exc) == [("amount_minor",)]


def test_charge_request_rejects_an_unknown_top_level_field() -> None:
    # Arrange
    payload = {**_payload(), "surprise": "x"}

    # Act
    with pytest.raises(ValidationError) as exc:
        ChargeRequest.model_validate(payload)

    # Assert
    assert [(error["type"], error["loc"]) for error in exc.value.errors()] == [
        ("extra_forbidden", ("surprise",))
    ]


def test_charge_request_rejects_an_unknown_card_field() -> None:
    # Arrange
    payload = _payload()
    card = dict(payload["card"])  # type: ignore[call-overload]
    card["holder"] = "Alice"
    payload["card"] = card

    # Act
    with pytest.raises(ValidationError) as exc:
        ChargeRequest.model_validate(payload)

    # Assert
    assert [(error["type"], error["loc"]) for error in exc.value.errors()] == [
        ("extra_forbidden", ("card", "holder"))
    ]
