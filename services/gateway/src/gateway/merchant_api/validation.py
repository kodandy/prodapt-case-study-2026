"""Request validation shared by the REST and gRPC merchant APIs: one rule set, field-level errors.

Descriptions never contain the submitted value, so a rejected PAN or CVC is not echoed by either transport.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import get_args

from pydantic import TypeAdapter, ValidationError
from pydantic_core import ErrorDetails

from gateway.payments.history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, PAYMENT_METHODS
from gateway.payments.schemas import ChargeRequest
from gateway.payments.state import PaymentState

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")
MAX_CURSOR_LENGTH = 512
PAYMENT_STATES: frozenset[str] = frozenset(get_args(PaymentState))
_TRANSPORT_SEGMENTS = frozenset({"body", "query", "path", "header"})
_STATE_ADAPTER: TypeAdapter[PaymentState] = TypeAdapter(PaymentState)


@dataclass(frozen=True)
class FieldViolation:
    field: str
    description: str


class RequestValidationFailedError(Exception):
    def __init__(self, violations: Sequence[FieldViolation]) -> None:
        super().__init__("request validation failed")
        self.violations: tuple[FieldViolation, ...] = tuple(violations)


@dataclass(frozen=True)
class PaymentHistoryQuery:
    limit: int
    cursor: str | None
    state: PaymentState | None
    created_from: datetime | None
    created_to: datetime | None
    method: str | None


def field_violations(errors: Sequence[ErrorDetails]) -> list[FieldViolation]:
    violations = []
    for error in errors:
        loc = list(error["loc"])
        if loc and loc[0] in _TRANSPORT_SEGMENTS:
            loc = loc[1:]
        violations.append(FieldViolation(".".join(str(part) for part in loc), error["msg"]))
    return violations


def validate_charge_request(payload: Mapping[str, object]) -> ChargeRequest:
    try:
        return ChargeRequest.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationFailedError(field_violations(exc.errors())) from None


def require_idempotency_key(value: str | None, label: str) -> str:
    """Visible ASCII only: a NUL would otherwise reach Postgres and fail as a 500."""
    if not value:
        raise RequestValidationFailedError([FieldViolation("idempotency_key", f"{label} is required")])
    if IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
        raise RequestValidationFailedError([FieldViolation("idempotency_key", f"invalid {label}")])
    return value


def _aware_utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _time_range(
    created_from: str | None, created_to: str | None, violations: list[FieldViolation]
) -> tuple[datetime | None, datetime | None]:
    start = None if created_from is None else _aware_utc(created_from)
    if created_from is not None and start is None:
        violations.append(FieldViolation("created_from", "invalid created_from"))
    end = None if created_to is None else _aware_utc(created_to)
    if created_to is not None and end is None:
        violations.append(FieldViolation("created_to", "invalid created_to"))
    if start is not None and end is not None and start >= end:
        violations.append(FieldViolation("created_to", "invalid date range"))
    return start, end


@dataclass(frozen=True)
class TransactionQuery:
    status: str | None
    created_from: datetime | None
    created_to: datetime | None
    method: str | None


def validate_transaction_query(
    *,
    status: str | None,
    created_from: str | None,
    created_to: str | None,
    method: str | None,
    allowed_statuses: frozenset[str],
    allowed_methods: frozenset[str],
) -> TransactionQuery:
    """The consumer `GET /v1/me/transactions` filters, with the same date-range rules as payment history."""
    violations: list[FieldViolation] = []
    start, end = _time_range(created_from, created_to, violations)
    if status is not None and status not in allowed_statuses:
        violations.append(FieldViolation("status", "invalid status"))
    if method is not None and method not in allowed_methods:
        violations.append(FieldViolation("method", "invalid method"))
    if violations:
        raise RequestValidationFailedError(violations)
    return TransactionQuery(status, start, end, method)


def validate_payment_history_query(
    *,
    limit: int | None,
    cursor: str | None,
    state: str | None,
    created_from: str | None,
    created_to: str | None,
    method: str | None,
) -> PaymentHistoryQuery:
    violations: list[FieldViolation] = []
    start, end = _time_range(created_from, created_to, violations)
    if method is not None and method not in PAYMENT_METHODS:
        violations.append(FieldViolation("method", "invalid method"))
    checked_state: PaymentState | None = None
    if state is not None:
        if state in PAYMENT_STATES:
            checked_state = _STATE_ADAPTER.validate_python(state)
        else:
            violations.append(FieldViolation("state", "invalid state"))
    page_size = DEFAULT_PAGE_SIZE if limit is None else limit
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        violations.append(FieldViolation("limit", "invalid limit"))
    if cursor is not None and len(cursor) > MAX_CURSOR_LENGTH:
        violations.append(FieldViolation("cursor", "invalid cursor"))
    if violations:
        raise RequestValidationFailedError(violations)
    return PaymentHistoryQuery(page_size, cursor, checked_state, start, end, method)
