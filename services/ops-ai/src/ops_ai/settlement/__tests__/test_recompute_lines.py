"""Tests for recompute_lines and the non_positive_amount code, from docs/specs/sq4.md H2 and H3 (pure)."""

import dataclasses
import uuid
from decimal import Decimal

import pytest
from ops_ai.settlement.contract import FX_SNAPSHOT, STANDARD_CONTRACT, Contract
from ops_ai.settlement.statement import (
    LINE_KINDS,
    EntryRow,
    LineKind,
    SettlementLine,
    build_lines,
    recompute_lines,
    verify_lines,
)

LINE_MISMATCH = ("line_mismatch",)


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
    """Converted to USD cents: 10000, 10870, 6700, 5000, 2174, 1190 (every line is non-zero)."""
    return (
        _entry(1, 10_000),
        _entry(2, 10_000, "EUR"),
        _entry(3, 10_000, "JPY", refunded=True),
        _entry(4, 5_000, chargeback=True),
        _entry(5, 2_000, "EUR", refunded=True, chargeback=True),
        _entry(6, 100_000, "INR", refunded=True),
    )


def _built(entries: tuple[EntryRow, ...]) -> tuple[SettlementLine, ...]:
    return build_lines(entries, STANDARD_CONTRACT, FX_SNAPSHOT)


def _recompute(lines: tuple[SettlementLine, ...], entries: tuple[EntryRow, ...]) -> tuple[str, ...]:
    return recompute_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)


def _tampered(
    lines: tuple[SettlementLine, ...], kind: LineKind, *, amount_delta: int = 0, count_delta: int = 0
) -> tuple[SettlementLine, ...]:
    return tuple(
        dataclasses.replace(
            line, amount_minor=line.amount_minor + amount_delta, count=line.count + count_delta
        )
        if line.kind == kind
        else line
        for line in lines
    )


def _line(lines: tuple[SettlementLine, ...], kind: LineKind) -> SettlementLine:
    return next(line for line in lines if line.kind == kind)


# --- H2: recompute_lines agrees with build_lines -----------------------------------------------------------


def test_recompute_lines_agrees_with_build_lines_for_the_mixed_entries() -> None:
    # Arrange
    entries = _mixed()
    lines = _built(entries)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == ()


def test_recompute_lines_agrees_with_build_lines_for_one_usd_payment_with_half_up_rounding() -> None:
    # Arrange: mdr 3580.05 -> 3580, reserve 6172.5 -> 6173
    entries = (_entry(1, 123_450),)
    lines = _built(entries)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == ()


def test_recompute_lines_agrees_for_no_entries_and_the_eight_zero_lines() -> None:
    # Arrange
    lines = _built(())

    # Act
    result = _recompute(lines, ())

    # Assert
    assert result == ()


def test_recompute_lines_skips_an_unsupported_currency_entry_as_build_lines_does() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 5_000, "GBP", chargeback=True))
    lines = _built(entries)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == ()


def test_recompute_lines_counts_a_refunded_and_charged_back_payment_as_a_chargeback_only() -> None:
    # Arrange
    entries = (_entry(1, 10_000, refunded=True, chargeback=True),)
    lines = _built(entries)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == ()


def test_recompute_lines_matches_by_kind_so_a_reordered_but_correct_set_of_lines_agrees() -> None:
    # Arrange: ordering is verify_lines' structural concern (line_order), not this function's
    entries = _mixed()
    lines = tuple(reversed(_built(entries)))

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == ()


# --- H2: recompute_lines catches a tampered line -----------------------------------------------------------


@pytest.mark.parametrize("kind", LINE_KINDS)
def test_recompute_lines_flags_a_line_whose_amount_differs_by_one(kind: LineKind) -> None:
    # Arrange
    entries = _mixed()
    lines = _tampered(_built(entries), kind, amount_delta=1)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == LINE_MISMATCH


@pytest.mark.parametrize("kind", LINE_KINDS)
def test_recompute_lines_flags_a_line_whose_count_differs_by_one(kind: LineKind) -> None:
    # Arrange
    entries = _mixed()
    lines = _tampered(_built(entries), kind, count_delta=1)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == LINE_MISMATCH


def test_recompute_lines_flags_lines_built_from_another_set_of_entries() -> None:
    # Arrange
    lines = _built((_entry(1, 10_000), _entry(2, 20_000)))
    entries = (_entry(1, 10_000), _entry(2, 20_001))

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert result == LINE_MISMATCH


@pytest.mark.parametrize(
    "contract",
    [
        dataclasses.replace(STANDARD_CONTRACT, mdr_bps=300),
        dataclasses.replace(STANDARD_CONTRACT, fixed_fee_minor=31),
        dataclasses.replace(STANDARD_CONTRACT, fx_markup_bps=250),
        dataclasses.replace(STANDARD_CONTRACT, chargeback_fee_minor=1_600),
        dataclasses.replace(STANDARD_CONTRACT, reserve_bps=600),
    ],
    ids=["mdr_bps", "fixed_fee_minor", "fx_markup_bps", "chargeback_fee_minor", "reserve_bps"],
)
def test_recompute_lines_uses_the_contract_it_is_given(contract: Contract) -> None:
    # Arrange
    entries = _mixed()
    lines = _built(entries)

    # Act
    result = recompute_lines(lines, entries, contract, FX_SNAPSHOT)

    # Assert
    assert result == LINE_MISMATCH


def test_recompute_lines_uses_the_fx_snapshot_it_is_given() -> None:
    # Arrange
    entries = _mixed()
    lines = _built(entries)
    other_fx = dataclasses.replace(
        FX_SNAPSHOT, usd_per_unit={**FX_SNAPSHOT.usd_per_unit, "EUR": Decimal("1.0900")}
    )

    # Act
    result = recompute_lines(lines, entries, STANDARD_CONTRACT, other_fx)

    # Assert
    assert result == LINE_MISMATCH


# --- H2: missing and duplicated kinds ----------------------------------------------------------------------


@pytest.mark.parametrize("kind", LINE_KINDS)
def test_recompute_lines_flags_a_missing_kind(kind: LineKind) -> None:
    # Arrange
    entries = _mixed()
    lines = tuple(line for line in _built(entries) if line.kind != kind)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert len(lines) == 7
    assert result == LINE_MISMATCH


@pytest.mark.parametrize("kind", LINE_KINDS)
def test_recompute_lines_flags_a_duplicated_kind_even_when_the_copy_is_correct(kind: LineKind) -> None:
    # Arrange
    entries = _mixed()
    built = _built(entries)
    lines = (*built, _line(built, kind))

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert len(lines) == 9
    assert result == LINE_MISMATCH


def test_recompute_lines_flags_eight_lines_where_one_kind_replaces_another() -> None:
    # Arrange: refunds is missing and chargebacks appears twice, so there are still eight lines
    entries = _mixed()
    built = _built(entries)
    lines = tuple(_line(built, "chargebacks") if line.kind == "refunds" else line for line in built)

    # Act
    result = _recompute(lines, entries)

    # Assert
    assert len(lines) == 8
    assert result == LINE_MISMATCH


def test_recompute_lines_flags_no_lines_at_all() -> None:
    # Arrange
    entries = _mixed()

    # Act
    result = _recompute((), entries)

    # Assert
    assert result == LINE_MISMATCH


def test_recompute_lines_flags_no_lines_even_without_entries() -> None:
    # Arrange / Act
    result = _recompute((), ())

    # Assert
    assert result == LINE_MISMATCH


# --- H3: verify_lines non_positive_amount ------------------------------------------------------------------


def test_verify_lines_accepts_the_smallest_positive_amount() -> None:
    # Arrange
    entries = (_entry(1, 1),)

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ()


def test_verify_lines_flags_an_entry_with_a_zero_amount() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 0))

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ("non_positive_amount",)


def test_verify_lines_flags_an_entry_with_a_negative_amount() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, -1))

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ("non_positive_amount",)


def test_verify_lines_flags_a_zero_amount_in_a_foreign_currency() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 0, "EUR"))

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ("non_positive_amount",)


def test_verify_lines_reports_non_positive_amount_once_for_several_bad_entries() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 0), _entry(3, 0, "EUR"), _entry(4, -1))

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ("non_positive_amount",)


def test_verify_lines_flags_a_non_positive_amount_whatever_its_currency_including_unsupported() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 0, "GBP"))

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ("unsupported_currency", "non_positive_amount")


def test_verify_lines_leaves_a_positive_unsupported_currency_entry_with_the_old_code_only() -> None:
    # Arrange
    entries = (_entry(1, 10_000), _entry(2, 5_000, "GBP"))

    # Act
    result = verify_lines(_built(entries), entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == ("unsupported_currency",)


def test_verify_lines_puts_non_positive_amount_last_after_every_other_code() -> None:
    # Arrange: GBP at 0 -> unsupported_currency + non_positive_amount; swapped mdr / reserve -> line_order;
    # fixed_fees > 0 -> sign; gross line 500 against 1000 recomputed -> gross_mismatch;
    # refunds 2000 > gross 500 -> returns_exceed_gross
    entries = (_entry(1, 1_000), _entry(2, 0, "GBP"))
    built = _built(entries)
    by_kind = {line.kind: line for line in built}
    lines = (
        SettlementLine("gross", 500, by_kind["gross"].count),
        by_kind["reserve"],
        SettlementLine("fixed_fees", 1, by_kind["fixed_fees"].count),
        by_kind["fx"],
        SettlementLine("refunds", -2_000, 1),
        by_kind["chargebacks"],
        by_kind["chargeback_fees"],
        by_kind["mdr"],
    )

    # Act
    result = verify_lines(lines, entries, STANDARD_CONTRACT, FX_SNAPSHOT)

    # Assert
    assert result == (
        "unsupported_currency",
        "line_order",
        "sign",
        "gross_mismatch",
        "returns_exceed_gross",
        "non_positive_amount",
    )
