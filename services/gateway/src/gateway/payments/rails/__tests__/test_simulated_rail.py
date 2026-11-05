"""Tests for `SimulatedRail`, written from docs/specs/gateway-basic.md section B1.4 / B2.3 / B2.5
(`gateway/payments/rails/`).

`RailResult` and `SimulatedRail` are imported from the `gateway.payments.rails` package, so the package
`__init__` must re-export them.
"""

import uuid

import pytest
from gateway.payments.rails import RailResult, SimulatedRail

SUCCEEDED = RailResult(status="succeeded", failure_code=None)
REJECTED = RailResult(status="failed", failure_code="rail_rejected")
UNKNOWN = RailResult(status="unknown", failure_code=None)


def _submit(
    rail: SimulatedRail,
    destination: str,
    payout_id: str | None = None,
    method: str = "bank_transfer",
    amount_minor: int = 2_500,
    currency: str = "USD",
) -> RailResult:
    return rail.submit(
        payout_id=payout_id or str(uuid.uuid4()),
        method=method,
        amount_minor=amount_minor,
        currency=currency,
        destination=destination,
    )


# --- outcomes ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("destination", ["DE89370400440532013000", "alice@okbank", "acct-0001"])
def test_submit_succeeds_for_a_destination_without_a_trigger_word(destination: str) -> None:
    # Arrange
    rail = SimulatedRail()

    # Act
    result = _submit(rail, destination)

    # Assert
    assert result == SUCCEEDED


@pytest.mark.parametrize("destination", ["fail", "acct-fail-0001", "myfail", "fail@upi"])
def test_submit_fails_with_rail_rejected_when_the_destination_contains_fail(destination: str) -> None:
    # Arrange
    rail = SimulatedRail()

    # Act
    result = _submit(rail, destination)

    # Assert
    assert result == REJECTED
    assert result.status == "failed"
    assert result.failure_code == "rail_rejected"


@pytest.mark.parametrize("destination", ["unknown", "acct-unknown-0001", "unknown@upi"])
def test_submit_returns_unknown_when_the_destination_contains_unknown(destination: str) -> None:
    # Arrange
    rail = SimulatedRail()

    # Act
    result = _submit(rail, destination)

    # Assert
    assert result == UNKNOWN
    assert result.status == "unknown"
    assert result.failure_code is None


@pytest.mark.parametrize("method", ["bank_transfer", "upi"])
def test_submit_decides_on_the_destination_whatever_the_method(method: str) -> None:
    # Arrange
    rail = SimulatedRail()

    # Act
    results = [
        _submit(rail, "acct-fail-0001", method=method),
        _submit(rail, "acct-unknown-0001", method=method),
        _submit(rail, "acct-ok-0001", method=method),
    ]

    # Assert
    assert results == [REJECTED, UNKNOWN, SUCCEEDED]


# --- repeat payout id -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("first_destination", "first_result", "second_destination"),
    [
        ("acct-ok-0001", SUCCEEDED, "acct-fail-0001"),
        ("acct-fail-0001", REJECTED, "acct-ok-0001"),
        ("acct-unknown-0001", UNKNOWN, "acct-ok-0001"),
        ("acct-ok-0001", SUCCEEDED, "acct-unknown-0001"),
    ],
    ids=["success_then_fail", "fail_then_success", "unknown_then_success", "success_then_unknown"],
)
def test_submit_with_a_repeated_payout_id_returns_the_first_answer(
    first_destination: str, first_result: RailResult, second_destination: str
) -> None:
    # Arrange
    rail = SimulatedRail()
    payout_id = str(uuid.uuid4())
    first = _submit(rail, first_destination, payout_id=payout_id)

    # Act
    second = _submit(rail, second_destination, payout_id=payout_id)

    # Assert
    assert first == first_result
    assert second == first_result


def test_submit_answers_each_payout_id_independently() -> None:
    # Arrange
    rail = SimulatedRail()
    id_a = str(uuid.uuid4())
    id_b = str(uuid.uuid4())

    # Act
    result_a = _submit(rail, "acct-fail-0001", payout_id=id_a)
    result_b = _submit(rail, "acct-ok-0001", payout_id=id_b)
    repeat_a = _submit(rail, "acct-ok-0001", payout_id=id_a)
    repeat_b = _submit(rail, "acct-fail-0001", payout_id=id_b)

    # Assert
    assert (result_a, repeat_a) == (REJECTED, REJECTED)
    assert (result_b, repeat_b) == (SUCCEEDED, SUCCEEDED)


def test_two_rail_instances_keep_separate_answers_for_the_same_payout_id() -> None:
    # Arrange
    payout_id = str(uuid.uuid4())
    first_rail = SimulatedRail()
    second_rail = SimulatedRail()
    _submit(first_rail, "acct-fail-0001", payout_id=payout_id)

    # Act
    result = _submit(second_rail, "acct-ok-0001", payout_id=payout_id)

    # Assert
    assert result == SUCCEEDED


# --- the result type --------------------------------------------------------------------------------------


def test_rail_result_failure_code_defaults_to_none() -> None:
    # Arrange / Act
    result = RailResult(status="succeeded")

    # Assert
    assert result.status == "succeeded"
    assert result.failure_code is None


def test_rail_result_is_frozen() -> None:
    # Arrange
    result = RailResult(status="succeeded")

    # Act / Assert
    with pytest.raises((AttributeError, ValueError)):
        result.status = "failed"  # type: ignore[misc]
    assert result.status == "succeeded"
