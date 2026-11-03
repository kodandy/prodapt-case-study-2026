"""Tests for build_lines and verify_lines, from docs/specs/sq4.md section 3 (pure, no database)."""

import dataclasses
import uuid
from collections.abc import Sequence

import pytest
from ops_ai.settlement.contract import FX_SNAPSHOT, STANDARD_CONTRACT, Contract
from ops_ai.settlement.statement import (
    EntryRow,
    LineKind,
    SettlementLine,
    build_lines,
    verify_lines,
)

KINDS: tuple[LineKind, ...] = (
    "gross",
    "mdr",
    "fixed_fees",
    "fx",
    "refunds",
    "chargebacks",
    "chargeback_fees",
    "reserve",
)


def _entry(
    n: int, amount_minor: int, currency: str = "USD", *, refunded: bool = False, chargeback: bool = False
) -> EntryRow:
    return EntryRow(
        payment_id=uuid.UUID(int=n),
        amount_minor=amount_minor,
        currency=currency,
        refunded=refunded,
        chargeback=chargeback,
    )


def _mixed() -> tuple[EntryRow, ...]:
    """Converted to USD cents: 10000, 10870, 6700, 5000, 2174, 1190."""
    return (
        _entry(1, 10_000),
        _entry(2, 10_000, "EUR"),
        _entry(3, 10_000, "JPY", refunded=True),
        _entry(4, 5_000, chargeback=True),
        _entry(5, 2_000, "EUR", refunded=True, chargeback=True),
        _entry(6, 100_000, "INR", refunded=True),
    )


def _as_tuples(lines: Sequence[SettlementLine]) -> list[tuple[str, int, int]]:
    return [(line.kind, line.amount_minor, line.count) for line in lines]


def _replace(
    lines: tuple[SettlementLine, ...], kind: LineKind, amount_minor: int
) -> tuple[SettlementLine, ...]:
    return tuple(
        dataclasses.replace(line, amount_minor=amount_minor) if line.kind == kind else line for line in lines
    )


def _swap(lines: tuple[SettlementLine, ...], first: LineKind, second: LineKind) -> tuple[SettlementLine, ...]:
    by_kind = {line.kind: line for line in lines}
    swapped = {first: by_kind[second], second: by_kind[first]}
    return tuple(swapped.get(line.kind, line) for line in lines)


# --- build_lines -----------------------------------------------------------------------------------------


def test_build_lines_returns_the_eight_kinds_in_order() -> None:
    # Arrange
    entries = _mixed()

    # Act
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert tuple(line.kind for line in lines) == KINDS


def test_build_lines_for_usd_entries_without_returns_has_zero_fx_refund_and_chargeback_lines() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 25_000), _entry(3, 4_995))

    # Act
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert: gross 39995; mdr 1159.855 -> 1160; reserve 1999.75 -> 2000
    assert _as_tuples(lines) == [
        ("gross", 39_995, 3),
        ("mdr", -1_160, 3),
        ("fixed_fees", -90, 3),
        ("fx", 0, 0),
        ("refunds", 0, 0),
        ("chargebacks", 0, 0),
        ("chargeback_fees", 0, 0),
        ("reserve", -2_000, 3),
    ]


def test_build_lines_for_a_mixed_set_converts_each_currency_and_derives_every_line() -> None:
    # Arrange
    entries = _mixed()

    # Act
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert: gross 35934; mdr 1042.086; fx base 20934 * 2% = 418.68; reserve 1796.7
    assert _as_tuples(lines) == [
        ("gross", 35_934, 6),
        ("mdr", -1_042, 6),
        ("fixed_fees", -180, 6),
        ("fx", -419, 4),
        ("refunds", -7_890, 2),
        ("chargebacks", -7_174, 2),
        ("chargeback_fees", -3_000, 2),
        ("reserve", -1_797, 6),
    ]


def test_build_lines_counts_a_refunded_and_charged_back_payment_as_a_chargeback_only() -> None:
    # Arrange
    entries = (_entry(1, 10_000, refunded=True, chargeback=True),)

    # Act
    lines = {line.kind: line for line in build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)}

    # Assert
    assert (lines["refunds"].amount_minor, lines["refunds"].count) == (0, 0)
    assert (lines["chargebacks"].amount_minor, lines["chargebacks"].count) == (-10_000, 1)
    assert (lines["chargeback_fees"].amount_minor, lines["chargeback_fees"].count) == (-1_500, 1)


def test_build_lines_never_takes_more_in_returns_than_the_captured_amount() -> None:
    # Arrange
    entries = (_entry(1, 10_000, refunded=True, chargeback=True), _entry(2, 2_000, "EUR", refunded=True))

    # Act
    lines = {line.kind: line for line in build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)}

    # Assert: 10000 once as a chargeback, 2174 once as a refund; gross is 12174
    assert -(lines["refunds"].amount_minor + lines["chargebacks"].amount_minor) == lines["gross"].amount_minor


def test_build_lines_counts_a_refund_without_a_chargeback_as_a_refund_with_no_chargeback_fee() -> None:
    # Arrange
    entries = (_entry(1, 10_000, refunded=True),)

    # Act
    lines = {line.kind: line for line in build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)}

    # Assert
    assert (lines["refunds"].amount_minor, lines["refunds"].count) == (-10_000, 1)
    assert (lines["chargebacks"].amount_minor, lines["chargebacks"].count) == (0, 0)
    assert (lines["chargeback_fees"].amount_minor, lines["chargeback_fees"].count) == (0, 0)


def test_build_lines_with_no_entries_returns_the_eight_kinds_all_zero() -> None:
    # Arrange / Act
    lines = build_lines((), STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert _as_tuples(lines) == [(kind, 0, 0) for kind in KINDS]


def test_build_lines_rounds_the_mdr_half_up_on_the_gross() -> None:
    # Arrange: 500 * 290 / 10_000 = 14.5
    entries = (_entry(1, 500),)

    # Act
    lines = {line.kind: line for line in build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)}

    # Assert
    assert lines["mdr"].amount_minor == -15


def test_build_lines_rounds_the_reserve_half_up_on_the_gross() -> None:
    # Arrange: 123450 * 500 / 10_000 = 6172.5
    entries = (_entry(1, 123_450),)

    # Act
    lines = {line.kind: line for line in build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)}

    # Assert
    assert lines["reserve"].amount_minor == -6_173


def test_build_lines_applies_the_contract_it_is_given() -> None:
    # Arrange
    contract = Contract(
        version="test-contract",
        settlement_currency="USD",
        mdr_bps=100,
        fixed_fee_minor=10,
        fx_markup_bps=1_000,
        chargeback_fee_minor=2_000,
        reserve_bps=1_000,
    )
    entries = (_entry(1, 10_000, "EUR", chargeback=True),)

    # Act
    lines = build_lines(entries, contract, FX_SNAPSHOT)

    # Assert: gross 10870; mdr 108.7 -> 109; fx 1087; reserve 1087
    assert _as_tuples(lines) == [
        ("gross", 10_870, 1),
        ("mdr", -109, 1),
        ("fixed_fees", -10, 1),
        ("fx", -1_087, 1),
        ("refunds", 0, 0),
        ("chargebacks", -10_870, 1),
        ("chargeback_fees", -2_000, 1),
        ("reserve", -1_087, 1),
    ]


def test_build_lines_leaves_an_unsupported_currency_out_of_every_line() -> None:
    # Arrange
    supported = _entry(1, 10_000)
    unsupported = _entry(2, 99_999, "GBP", refunded=True, chargeback=True)

    # Act
    with_unsupported = build_lines((supported, unsupported), STANDARD_CONTRACT, FX_SNAPSHOT)
    without = build_lines((supported,), STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert with_unsupported == without
    assert _as_tuples(with_unsupported)[0] == ("gross", 10_000, 1)


def test_build_lines_does_not_depend_on_the_order_of_the_entries() -> None:
    # Arrange
    entries = list(_mixed())
    shuffled = entries[1::2] + entries[0::2][::-1]

    # Act
    lines = build_lines(tuple(shuffled), STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert lines == build_lines(tuple(entries), STANDARD_CONTRACT, FX_SNAPSHOT)


def test_build_lines_gross_is_positive_and_every_other_line_is_not() -> None:
    # Arrange
    entries = _mixed()

    # Act
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert [line.kind for line in lines if line.amount_minor > 0] == ["gross"]


# --- verify_lines ----------------------------------------------------------------------------------------


def test_verify_lines_accepts_the_lines_built_from_the_same_entries() -> None:
    # Arrange
    entries = _mixed()
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ()


def test_verify_lines_accepts_the_all_zero_lines_of_no_entries() -> None:
    # Arrange
    lines = build_lines((), STANDARD_CONTRACT, FX_SNAPSHOT)

    # Act
    mismatches = verify_lines(lines, (), STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ()


def test_verify_lines_flags_an_unsupported_currency() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 5_000, "GBP"))
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("unsupported_currency",)


def test_verify_lines_flags_unsupported_currency_once_for_several_unsupported_entries() -> None:
    # Arrange
    entries = (_entry(1, 5_000, "GBP"), _entry(2, 6_000, "CHF"), _entry(3, 7_000, "GBP"))
    lines = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("unsupported_currency",)


def test_verify_lines_flags_two_lines_out_of_order() -> None:
    # Arrange
    entries = _mixed()
    lines = _swap(build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT), "fx", "chargebacks")

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert tuple(line.kind for line in lines)[3] == "chargebacks"
    assert mismatches == ("line_order",)


def test_verify_lines_flags_a_missing_line() -> None:
    # Arrange
    entries = _mixed()
    lines = tuple(
        line for line in build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT) if line.kind != "reserve"
    )

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("line_order",)


def test_verify_lines_flags_an_extra_line() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = (*built, SettlementLine(kind="reserve", amount_minor=0, count=0))

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("line_order",)


@pytest.mark.parametrize(
    "kind", ["mdr", "fixed_fees", "fx", "refunds", "chargebacks", "chargeback_fees", "reserve"]
)
def test_verify_lines_flags_a_positive_non_gross_line_as_a_sign_error(kind: LineKind) -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(built, kind, amount_minor=1)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("sign",)


def test_verify_lines_reports_sign_once_for_several_positive_lines() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(_replace(built, "mdr", amount_minor=5), "reserve", amount_minor=5)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("sign",)


def test_verify_lines_flags_a_negative_gross_as_a_sign_error_before_the_gross_mismatch() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(built, "gross", amount_minor=-35_934)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches.count("sign") == 1
    assert mismatches.index("sign") < mismatches.index("gross_mismatch")


def test_verify_lines_flags_a_gross_that_is_one_unit_too_high() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(built, "gross", amount_minor=35_935)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("gross_mismatch",)


def test_verify_lines_flags_a_gross_that_is_one_unit_too_low() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(built, "gross", amount_minor=35_933)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("gross_mismatch",)


def test_verify_lines_flags_a_gross_computed_from_other_entries() -> None:
    # Arrange
    entries = _mixed()
    lines = build_lines((*entries, _entry(7, 100)), STANDARD_CONTRACT, FX_SNAPSHOT)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("gross_mismatch",)


def test_verify_lines_accepts_returns_equal_to_the_gross() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(_replace(built, "refunds", amount_minor=-30_000), "chargebacks", amount_minor=-5_934)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ()


def test_verify_lines_flags_returns_one_unit_over_the_gross() -> None:
    # Arrange
    entries = _mixed()
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    lines = _replace(_replace(built, "refunds", amount_minor=-30_000), "chargebacks", amount_minor=-5_935)

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == ("returns_exceed_gross",)


def test_verify_lines_reports_every_code_once_and_in_the_specified_order() -> None:
    # Arrange: GBP entry; fx and chargebacks swapped; reserve and mdr positive; gross off; returns above gross
    entries = (*_mixed(), _entry(7, 100, "GBP"))
    built = build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)
    tampered = _replace(built, "gross", amount_minor=35_935)
    tampered = _replace(tampered, "mdr", amount_minor=5)
    tampered = _replace(tampered, "reserve", amount_minor=5)
    tampered = _replace(tampered, "refunds", amount_minor=-40_000)
    lines = _swap(tampered, "fx", "chargebacks")

    # Act
    mismatches = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert mismatches == (
        "unsupported_currency",
        "line_order",
        "sign",
        "gross_mismatch",
        "returns_exceed_gross",
    )
