import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from ops_ai.settlement.contract import Contract, FxSnapshot, apply_bps, is_supported, to_settlement_minor

LineKind = Literal["gross", "mdr", "fixed_fees", "fx", "refunds", "chargebacks", "chargeback_fees", "reserve"]
LINE_KINDS: tuple[LineKind, ...] = (
    "gross",
    "mdr",
    "fixed_fees",
    "fx",
    "refunds",
    "chargebacks",
    "chargeback_fees",
    "reserve",
)


@dataclass(frozen=True)
class SettlementLine:
    kind: LineKind
    amount_minor: int
    """Settlement-currency minor units; gross is positive, every other line is zero or negative."""
    count: int


@dataclass(frozen=True)
class EntryRow:
    payment_id: uuid.UUID
    amount_minor: int
    currency: str
    refunded: bool
    chargeback: bool


@dataclass(frozen=True)
class SettlementStatement:
    period: str
    currency: str
    contract_version: str
    fx_rate_id: str
    lines: tuple[SettlementLine, ...]
    gross_minor: int
    expected_minor: int
    """gross + mdr: what a merchant computes naively."""
    net_minor: int
    gap_minor: int
    """expected - net."""
    verified: bool
    mismatches: tuple[str, ...]
    projection_checked: int


def _converted(entries: Sequence[EntryRow], fx: FxSnapshot) -> list[tuple[EntryRow, int]]:
    return [
        (entry, to_settlement_minor(entry.amount_minor, entry.currency, fx))
        for entry in entries
        if is_supported(entry.currency, fx)
    ]


def build_lines(
    entries: Sequence[EntryRow], contract: Contract, fx: FxSnapshot
) -> tuple[SettlementLine, ...]:
    """The eight lines in LINE_KINDS order. Entries in an unsupported currency are left out of every line."""
    converted = _converted(entries, fx)
    count = len(converted)
    gross = sum(amount for _entry, amount in converted)
    foreign = [amount for entry, amount in converted if entry.currency != contract.settlement_currency]
    charged_back = [amount for entry, amount in converted if entry.chargeback]
    refunded = [amount for entry, amount in converted if entry.refunded and not entry.chargeback]
    return (
        SettlementLine("gross", gross, count),
        SettlementLine("mdr", -apply_bps(gross, contract.mdr_bps), count),
        SettlementLine("fixed_fees", -count * contract.fixed_fee_minor, count),
        SettlementLine("fx", -apply_bps(sum(foreign), contract.fx_markup_bps), len(foreign)),
        SettlementLine("refunds", -sum(refunded), len(refunded)),
        SettlementLine("chargebacks", -sum(charged_back), len(charged_back)),
        SettlementLine(
            "chargeback_fees", -len(charged_back) * contract.chargeback_fee_minor, len(charged_back)
        ),
        SettlementLine("reserve", -apply_bps(gross, contract.reserve_bps), count),
    )


def _total(lines: Sequence[SettlementLine], kind: LineKind) -> int:
    return sum(line.amount_minor for line in lines if line.kind == kind)


def verify_lines(
    lines: Sequence[SettlementLine], entries: Sequence[EntryRow], contract: Contract, fx: FxSnapshot
) -> tuple[str, ...]:
    """Mismatch codes in a fixed order, each at most once; () means verified."""
    mismatches: list[str] = []
    if any(not is_supported(entry.currency, fx) for entry in entries):
        mismatches.append("unsupported_currency")
    if tuple(line.kind for line in lines) != LINE_KINDS:
        mismatches.append("line_order")
    if any(
        (line.kind == "gross" and line.amount_minor < 0) or (line.kind != "gross" and line.amount_minor > 0)
        for line in lines
    ):
        mismatches.append("sign")
    gross = _total(lines, "gross")
    if gross != sum(amount for _entry, amount in _converted(entries, fx)):
        mismatches.append("gross_mismatch")
    if -(_total(lines, "refunds") + _total(lines, "chargebacks")) > gross:
        mismatches.append("returns_exceed_gross")
    if any(entry.amount_minor <= 0 for entry in entries):
        mismatches.append("non_positive_amount")
    return tuple(mismatches)


def recompute_lines(
    lines: Sequence[SettlementLine], entries: Sequence[EntryRow], contract: Contract, fx: FxSnapshot
) -> tuple[str, ...]:
    """("line_mismatch",) unless every kind appears once, with the amount and count recomputed per payment.

    Deliberately does not reuse build_lines, so a defect there does not hide in its own check.
    """
    gross = count = foreign = foreign_count = 0
    fixed_fees = refunds = refund_count = chargebacks = chargeback_count = chargeback_fees = 0
    for entry in entries:
        if not is_supported(entry.currency, fx):
            continue
        amount = to_settlement_minor(entry.amount_minor, entry.currency, fx)
        gross += amount
        count += 1
        fixed_fees -= contract.fixed_fee_minor
        if entry.currency != contract.settlement_currency:
            foreign += amount
            foreign_count += 1
        if entry.chargeback:
            chargebacks -= amount
            chargeback_count += 1
            chargeback_fees -= contract.chargeback_fee_minor
        elif entry.refunded:
            refunds -= amount
            refund_count += 1
    expected: dict[LineKind, tuple[int, int]] = {
        "gross": (gross, count),
        "mdr": (-apply_bps(gross, contract.mdr_bps), count),
        "fixed_fees": (fixed_fees, count),
        "fx": (-apply_bps(foreign, contract.fx_markup_bps), foreign_count),
        "refunds": (refunds, refund_count),
        "chargebacks": (chargebacks, chargeback_count),
        "chargeback_fees": (chargeback_fees, chargeback_count),
        "reserve": (-apply_bps(gross, contract.reserve_bps), count),
    }
    actual = {line.kind: (line.amount_minor, line.count) for line in lines}
    if len(lines) != len(actual) or actual != expected:
        return ("line_mismatch",)
    return ()


def make_statement(
    period: str,
    contract: Contract,
    fx: FxSnapshot,
    lines: tuple[SettlementLine, ...],
    mismatches: tuple[str, ...],
    projection_checked: int,
) -> SettlementStatement:
    """net, expected and gap are derived from the lines, so they cannot disagree with them."""
    gross = _total(lines, "gross")
    expected = gross + _total(lines, "mdr")
    net = sum(line.amount_minor for line in lines)
    return SettlementStatement(
        period=period,
        currency=contract.settlement_currency,
        contract_version=contract.version,
        fx_rate_id=fx.rate_id,
        lines=lines,
        gross_minor=gross,
        expected_minor=expected,
        net_minor=net,
        gap_minor=expected - net,
        verified=mismatches == (),
        mismatches=mismatches,
        projection_checked=projection_checked,
    )
