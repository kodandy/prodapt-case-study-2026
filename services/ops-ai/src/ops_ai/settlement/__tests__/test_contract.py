"""Tests for ops_ai/settlement/contract.py, from docs/specs/sq4.md section 1 (pure, no database)."""

import dataclasses
from decimal import Decimal

import pytest
from ops_ai.settlement.contract import (
    FX_SNAPSHOT,
    MINOR_EXPONENT,
    STANDARD_CONTRACT,
    Contract,
    FxSnapshot,
    UnsupportedCurrencyError,
    apply_bps,
    to_settlement_minor,
)


def _assign(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


# --- the constants ---------------------------------------------------------------------------------------


def test_the_standard_contract_has_the_specified_terms() -> None:
    # Arrange / Act
    contract = STANDARD_CONTRACT

    # Assert
    assert contract == Contract(
        version="std-2026-10",
        settlement_currency="USD",
        mdr_bps=290,
        fixed_fee_minor=30,
        fx_markup_bps=200,
        chargeback_fee_minor=1500,
        reserve_bps=500,
    )


def test_the_fx_snapshot_has_the_specified_rate_id_and_decimal_rates() -> None:
    # Arrange / Act
    snapshot = FX_SNAPSHOT

    # Assert
    assert snapshot.rate_id == "fx-2026-10"
    assert dict(snapshot.usd_per_unit) == {
        "USD": Decimal("1"),
        "EUR": Decimal("1.0870"),
        "INR": Decimal("0.0119"),
        "JPY": Decimal("0.0067"),
    }
    assert {type(rate) for rate in snapshot.usd_per_unit.values()} == {Decimal}


def test_the_minor_exponents_are_two_except_for_the_yen() -> None:
    # Arrange / Act
    exponents = dict(MINOR_EXPONENT)

    # Assert
    assert exponents == {"USD": 2, "EUR": 2, "INR": 2, "JPY": 0}


def test_the_contract_and_the_snapshot_are_frozen_dataclasses() -> None:
    # Arrange
    contract = STANDARD_CONTRACT
    snapshot = FX_SNAPSHOT

    # Act / Assert
    assert dataclasses.is_dataclass(contract)
    assert dataclasses.is_dataclass(snapshot)
    with pytest.raises(dataclasses.FrozenInstanceError):
        _assign(contract, "mdr_bps", 1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        _assign(snapshot, "rate_id", "other")


# --- to_settlement_minor ---------------------------------------------------------------------------------


@pytest.mark.parametrize("amount_minor", [0, 1, 123_450, 9_007_199_254_740_993])
def test_to_settlement_minor_leaves_usd_unchanged(amount_minor: int) -> None:
    # Arrange / Act
    converted = to_settlement_minor(amount_minor, "USD")

    # Assert
    assert converted == amount_minor


@pytest.mark.parametrize(
    ("amount_minor", "currency", "expected"),
    [
        (10_000, "EUR", 10_870),
        (499, "EUR", 542),  # 542.413 rounds down
        (501, "EUR", 545),  # 544.587 rounds up
        (500, "EUR", 544),  # 543.5 is a tie: half-up
        (1_500, "EUR", 1_631),  # 1630.5 is a tie: half-up (half-even would give 1630)
        (2_500, "EUR", 2_718),  # 2717.5 is a tie: half-up
        (1, "EUR", 1),  # 1.087
        (100, "JPY", 67),  # 67.0 exactly; yen have no minor unit
        (50, "JPY", 34),  # 33.5 is a tie: half-up
        (150, "JPY", 101),  # 100.5 is a tie: half-up (half-even would give 100)
        (1, "JPY", 1),  # 0.67 rounds up
        (1_000, "INR", 12),  # 11.9
        (1_500, "INR", 18),  # 17.85
        (42, "INR", 0),  # 0.4998 rounds down to zero
        (43, "INR", 1),  # 0.5117 rounds up
        (0, "EUR", 0),
        (0, "JPY", 0),
    ],
)
def test_to_settlement_minor_converts_through_the_snapshot_and_rounds_half_up(
    amount_minor: int, currency: str, expected: int
) -> None:
    # Arrange / Act
    converted = to_settlement_minor(amount_minor, currency)

    # Assert
    assert converted == expected


@pytest.mark.parametrize(
    ("currency", "expected"),
    [("EUR", 9_790_825_589_903_459), ("JPY", 6_034_823_500_676_465), ("INR", 107_185_671_131_418)],
)
def test_to_settlement_minor_is_exact_beyond_float_precision(currency: str, expected: int) -> None:
    # Arrange: 2**53 + 1 is the first integer a float cannot hold
    amount_minor = 9_007_199_254_740_993

    # Act
    converted = to_settlement_minor(amount_minor, currency)

    # Assert
    assert converted == expected
    assert isinstance(converted, int)


def test_to_settlement_minor_uses_the_snapshot_it_is_given() -> None:
    # Arrange
    snapshot = FxSnapshot("fx-test", {"EUR": Decimal("2"), "USD": Decimal("1")})

    # Act
    converted = to_settlement_minor(100, "EUR", snapshot)

    # Assert
    assert converted == 200


@pytest.mark.parametrize("currency", ["GBP", "CHF", "", "XYZ"])
def test_to_settlement_minor_rejects_a_currency_missing_from_both_tables(currency: str) -> None:
    # Arrange / Act / Assert
    with pytest.raises(UnsupportedCurrencyError) as raised:
        to_settlement_minor(100, currency)
    assert raised.value.args == (currency,)


def test_to_settlement_minor_rejects_a_currency_that_has_a_rate_but_no_exponent() -> None:
    # Arrange
    snapshot = FxSnapshot("fx-test", {"GBP": Decimal("1.30"), "USD": Decimal("1")})

    # Act / Assert
    with pytest.raises(UnsupportedCurrencyError) as raised:
        to_settlement_minor(100, "GBP", snapshot)
    assert raised.value.args == ("GBP",)


def test_to_settlement_minor_rejects_a_currency_that_has_an_exponent_but_no_rate() -> None:
    # Arrange
    snapshot = FxSnapshot("fx-test", {"USD": Decimal("1")})

    # Act / Assert
    with pytest.raises(UnsupportedCurrencyError) as raised:
        to_settlement_minor(100, "EUR", snapshot)
    assert raised.value.args == ("EUR",)


def test_unsupported_currency_error_is_an_exception() -> None:
    # Arrange / Act
    error = UnsupportedCurrencyError("GBP")

    # Assert
    assert isinstance(error, Exception)
    assert error.args == ("GBP",)


# --- apply_bps -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount_minor", "bps", "expected"),
    [
        (1_000, 290, 29),  # 29.0 exactly
        (50, 290, 1),  # 1.45 rounds down
        (52, 290, 2),  # 1.508 rounds up
        (1, 5_000, 1),  # 0.5 is a tie: half-up
        (3, 5_000, 2),  # 1.5 is a tie: half-up
        (25, 200, 1),  # 0.5 is a tie: half-up
        (75, 200, 2),  # 1.5 is a tie: half-up
        (125, 200, 3),  # 2.5 is a tie: half-up (half-even would give 2)
        (500, 290, 15),  # 14.5 is a tie: half-up (half-even would give 14)
        (2_500, 290, 73),  # 72.5 is a tie: half-up (half-even would give 72)
        (123_450, 500, 6_173),  # 6172.5 is a tie: half-up (half-even would give 6172)
        (49_999, 1, 5),  # 4.9999
        (4_999, 1, 0),  # 0.4999
        (0, 290, 0),
        (100, 0, 0),
        (10_000, 10_000, 10_000),
    ],
)
def test_apply_bps_takes_basis_points_and_rounds_half_up(amount_minor: int, bps: int, expected: int) -> None:
    # Arrange / Act
    result = apply_bps(amount_minor, bps)

    # Assert
    assert result == expected


def test_apply_bps_is_exact_beyond_float_precision() -> None:
    # Arrange: (2**53 + 1) * 290 / 10_000 = 261208778387488.797
    amount_minor = 9_007_199_254_740_993

    # Act
    result = apply_bps(amount_minor, 290)

    # Assert
    assert result == 261_208_778_387_489
    assert isinstance(result, int)
