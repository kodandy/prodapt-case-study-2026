from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal

# Wide enough that no realistic minor amount is rounded before the final half-up step.
_EXACT = Context(prec=80)
_ONE = Decimal(1)


class UnsupportedCurrencyError(Exception):
    """The currency is missing from the FX snapshot or from the minor-exponent table."""


@dataclass(frozen=True)
class Contract:
    version: str
    settlement_currency: str
    mdr_bps: int
    fixed_fee_minor: int
    fx_markup_bps: int
    chargeback_fee_minor: int
    reserve_bps: int


@dataclass(frozen=True)
class FxSnapshot:
    rate_id: str
    usd_per_unit: Mapping[str, Decimal]


STANDARD_CONTRACT = Contract(
    version="std-2026-10",
    settlement_currency="USD",
    mdr_bps=290,
    fixed_fee_minor=30,
    fx_markup_bps=200,
    chargeback_fee_minor=1500,
    reserve_bps=500,
)
FX_SNAPSHOT = FxSnapshot(
    rate_id="fx-2026-10",
    usd_per_unit={
        "USD": Decimal("1"),
        "EUR": Decimal("1.0870"),
        "INR": Decimal("0.0119"),
        "JPY": Decimal("0.0067"),
    },
)
MINOR_EXPONENT: Mapping[str, int] = {"USD": 2, "EUR": 2, "INR": 2, "JPY": 0}
SETTLEMENT_EXPONENT = 2


def _half_up(value: Decimal) -> int:
    return int(value.quantize(_ONE, rounding=ROUND_HALF_UP, context=_EXACT))


def is_supported(currency: str, fx: FxSnapshot = FX_SNAPSHOT) -> bool:
    return currency in MINOR_EXPONENT and currency in fx.usd_per_unit


def to_settlement_minor(amount_minor: int, currency: str, fx: FxSnapshot = FX_SNAPSHOT) -> int:
    """amount / 10**exponent * USD per unit * 100, rounded half-up to whole USD cents."""
    if not is_supported(currency, fx):
        raise UnsupportedCurrencyError(currency)
    scaled = _EXACT.multiply(
        _EXACT.multiply(Decimal(amount_minor), fx.usd_per_unit[currency]), Decimal(10) ** SETTLEMENT_EXPONENT
    )
    return _half_up(_EXACT.divide(scaled, Decimal(10) ** MINOR_EXPONENT[currency]))


def apply_bps(amount_minor: int, bps: int) -> int:
    return _half_up(_EXACT.divide(Decimal(amount_minor * bps), Decimal(10_000)))
