"""B8.3: one validation module for REST and gRPC (contract: scratchpad gwgrpc-contract.md section 1)."""

from datetime import UTC, datetime

import pytest
from gateway.merchant_api.validation import (
    FieldViolation,
    PaymentHistoryQuery,
    RequestValidationFailedError,
    field_violations,
    require_idempotency_key,
    validate_charge_request,
    validate_payment_history_query,
)
from gateway.payments.schemas import ChargeRequest
from pydantic import ValidationError
from pydantic_core import ErrorDetails

APPROVE_PAN = "4242424242424242"
NON_TEST_LUHN_PAN = "4111111111111111"
BAD_LUHN_PAN = "4242424242424241"


def _payload(**overrides: object) -> dict[str, object]:
    card: dict[str, object] = {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"}
    payload: dict[str, object] = {"amount_minor": 1500, "currency": "USD", "method": "card", "card": card}
    for key, value in overrides.items():
        if key.startswith("card_"):
            card[key.removeprefix("card_")] = value
        else:
            payload[key] = value
    return payload


def _violations(payload: dict[str, object]) -> tuple[FieldViolation, ...]:
    with pytest.raises(RequestValidationFailedError) as excinfo:
        validate_charge_request(payload)
    return excinfo.value.violations


def _fields(violations: tuple[FieldViolation, ...]) -> list[str]:
    return [v.field for v in violations]


def _history_violations(
    *,
    limit: int | None = None,
    cursor: str | None = None,
    state: str | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    method: str | None = None,
) -> list[tuple[str, str]]:
    with pytest.raises(RequestValidationFailedError) as excinfo:
        validate_payment_history_query(
            limit=limit,
            cursor=cursor,
            state=state,
            created_from=created_from,
            created_to=created_to,
            method=method,
        )
    return [(v.field, v.description) for v in excinfo.value.violations]


# --- validate_charge_request -------------------------------------------------------------------------------


def test_validate_charge_request_returns_the_charge_request_for_a_valid_payload() -> None:
    # Arrange
    payload = _payload()

    # Act
    request = validate_charge_request(payload)

    # Assert
    assert isinstance(request, ChargeRequest)
    assert (request.amount_minor, request.currency, request.method) == (1500, "USD", "card")
    assert request.card.number.get_secret_value() == APPROVE_PAN


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"currency": "GBP"}, "currency"),
        ({"amount_minor": 0}, "amount_minor"),
        ({"amount_minor": -5}, "amount_minor"),
        ({"amount_minor": 10**12 + 1}, "amount_minor"),
        ({"card_number": BAD_LUHN_PAN}, "card.number"),
        ({"card_number": NON_TEST_LUHN_PAN}, "card.number"),
        ({"card_number": "4" * 64}, "card.number"),
        ({"card_cvc": "12345"}, "card.cvc"),
        ({"card_cvc": "12a"}, "card.cvc"),
        ({"card_exp_month": 13}, "card.exp_month"),
        ({"method": "wire"}, "method"),
    ],
)
def test_validate_charge_request_reports_the_offending_field(
    overrides: dict[str, object], field: str
) -> None:
    # Arrange
    payload = _payload(**overrides)

    # Act
    violations = _violations(payload)

    # Assert
    assert _fields(violations) == [field]
    assert violations[0].description != ""


def test_validate_charge_request_collects_every_invalid_field_in_order() -> None:
    # Arrange
    payload = _payload(amount_minor=0, currency="GBP")

    # Act
    violations = _violations(payload)

    # Assert
    assert _fields(violations) == ["amount_minor", "currency"]


def test_validate_charge_request_reports_missing_card_and_unknown_fields() -> None:
    # Arrange
    payload = _payload()
    del payload["card"]
    payload["surprise"] = 1

    # Act
    violations = _violations(payload)

    # Assert
    assert sorted(_fields(violations)) == ["card", "surprise"]


@pytest.mark.parametrize("pan", [BAD_LUHN_PAN, NON_TEST_LUHN_PAN, "4" * 64])
def test_validate_charge_request_never_echoes_the_card_number(pan: str) -> None:
    # Arrange
    payload = _payload(card_number=pan)

    # Act
    violations = _violations(payload)

    # Assert
    rendered = repr(violations) + "".join(v.description for v in violations)
    assert pan not in rendered


def test_validate_charge_request_never_echoes_the_cvc() -> None:
    # Arrange
    payload = _payload(card_cvc="98765")

    # Act
    violations = _violations(payload)

    # Assert
    assert "98765" not in repr(violations)


# --- field_violations ---------------------------------------------------------------------------------


def _errors(loc: tuple[str | int, ...], msg: str) -> list[ErrorDetails]:
    return [{"type": "value_error", "loc": loc, "msg": msg, "input": None}]


@pytest.mark.parametrize(
    ("loc", "field"),
    [
        (("body", "card", "number"), "card.number"),
        (("query", "limit"), "limit"),
        (("path", "payment_id"), "payment_id"),
        (("header", "idempotency-key"), "idempotency-key"),
        (("amount_minor",), "amount_minor"),
        (("card", "number"), "card.number"),
        (("body", "items", 0, "x"), "items.0.x"),
    ],
)
def test_field_violations_drops_one_transport_segment_and_dot_joins(
    loc: tuple[str | int, ...], field: str
) -> None:
    # Arrange
    errors = _errors(loc, "bad value")

    # Act
    violations = field_violations(errors)

    # Assert
    assert violations == [FieldViolation(field=field, description="bad value")]


def test_field_violations_keeps_pydantic_order() -> None:
    # Arrange
    with pytest.raises(ValidationError) as excinfo:
        ChargeRequest.model_validate(_payload(amount_minor=0, currency="GBP"))
    errors = excinfo.value.errors()

    # Act
    violations = field_violations(errors)

    # Assert
    assert [v.field for v in violations] == ["amount_minor", "currency"]
    assert [v.description for v in violations] == [e["msg"] for e in errors]


# --- require_idempotency_key -----------------------------------------------------------------------------


def test_require_idempotency_key_returns_a_valid_key() -> None:
    # Arrange
    key = "order-42_retry~1"

    # Act
    result = require_idempotency_key(key, "Idempotency-Key header")

    # Assert
    assert result == key


@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("label", ["Idempotency-Key header", "idempotency-key metadata"])
def test_require_idempotency_key_reports_a_missing_key_with_the_transport_label(
    value: str | None, label: str
) -> None:
    # Arrange / Act
    with pytest.raises(RequestValidationFailedError) as excinfo:
        require_idempotency_key(value, label)

    # Assert
    assert excinfo.value.violations == (FieldViolation("idempotency_key", f"{label} is required"),)


@pytest.mark.parametrize("value", ["has space", "k" * 256, "café", "tab\tkey"])
def test_require_idempotency_key_rejects_non_visible_ascii_or_oversize(value: str) -> None:
    # Arrange
    label = "idempotency-key metadata"

    # Act
    with pytest.raises(RequestValidationFailedError) as excinfo:
        require_idempotency_key(value, label)

    # Assert
    expected = FieldViolation("idempotency_key", "invalid idempotency-key metadata")
    assert excinfo.value.violations == (expected,)


def test_require_idempotency_key_accepts_the_255_character_maximum() -> None:
    # Arrange
    key = "k" * 255

    # Act
    result = require_idempotency_key(key, "Idempotency-Key header")

    # Assert
    assert result == key


# --- validate_payment_history_query ------------------------------------------------------------------------


def test_validate_payment_history_query_defaults_and_converts_to_utc() -> None:
    # Arrange / Act
    query = validate_payment_history_query(
        limit=None,
        cursor="abc",
        state="succeeded",
        created_from="2026-11-01T09:00:00+09:00",
        created_to="2026-11-02T00:00:00Z",
        method="card",
    )

    # Assert
    assert query == PaymentHistoryQuery(
        limit=20,
        cursor="abc",
        state="succeeded",
        created_from=datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
        created_to=datetime(2026, 11, 2, 0, 0, tzinfo=UTC),
        method="card",
    )
    assert query.created_from is not None and query.created_from.utcoffset() is not None


def test_validate_payment_history_query_with_no_filters() -> None:
    # Arrange / Act
    query = validate_payment_history_query(
        limit=100, cursor=None, state=None, created_from=None, created_to=None, method=None
    )

    # Assert
    assert query == PaymentHistoryQuery(
        limit=100, cursor=None, state=None, created_from=None, created_to=None, method=None
    )


@pytest.mark.parametrize(
    "value", ["2026-11-01T00:00:00", "2026-11-01", "yesterday", "9999-12-31T23:59:59-23:59"]
)
def test_validate_payment_history_query_rejects_invalid_created_from(value: str) -> None:
    # Arrange / Act
    violations = _history_violations(created_from=value)

    # Assert
    assert violations == [("created_from", "invalid created_from")]


def test_validate_payment_history_query_rejects_invalid_created_to() -> None:
    # Arrange / Act
    violations = _history_violations(created_to="2026-11-01T00:00:00")

    # Assert
    assert violations == [("created_to", "invalid created_to")]


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-11-02T00:00:00Z", "2026-11-01T00:00:00Z"), ("2026-11-01T00:00:00Z", "2026-11-01T00:00:00Z")],
)
def test_validate_payment_history_query_rejects_an_empty_or_reversed_range(start: str, end: str) -> None:
    # Arrange / Act
    violations = _history_violations(created_from=start, created_to=end)

    # Assert
    assert violations == [("created_to", "invalid date range")]


@pytest.mark.parametrize("method", ["CARD", "wallet"])
def test_validate_payment_history_query_rejects_an_unknown_method(method: str) -> None:
    # Arrange / Act
    violations = _history_violations(method=method)

    # Assert
    assert violations == [("method", "invalid method")]


def test_validate_payment_history_query_rejects_an_unknown_state() -> None:
    # Arrange / Act
    violations = _history_violations(state="settled")

    # Assert
    assert violations == [("state", "invalid state")]


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_validate_payment_history_query_rejects_a_limit_outside_1_to_100(limit: int) -> None:
    # Arrange / Act
    violations = _history_violations(limit=limit)

    # Assert
    assert violations == [("limit", "invalid limit")]


def test_validate_payment_history_query_rejects_an_oversize_cursor() -> None:
    # Arrange
    expected = [("cursor", "invalid cursor")]

    # Act
    violations = _history_violations(cursor="c" * 513)

    # Assert
    assert violations == expected


def test_validate_payment_history_query_reports_all_violations_in_contract_order() -> None:
    # Arrange / Act
    violations = _history_violations(
        created_from="bad",
        created_to="bad",
        method="wire",
        state="nope",
        limit=0,
        cursor="c" * 600,
    )

    # Assert
    assert violations == [
        ("created_from", "invalid created_from"),
        ("created_to", "invalid created_to"),
        ("method", "invalid method"),
        ("state", "invalid state"),
        ("limit", "invalid limit"),
        ("cursor", "invalid cursor"),
    ]
