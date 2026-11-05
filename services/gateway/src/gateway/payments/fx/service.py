import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gateway.payments.fx.models import FxRate
from gateway.payments.ledger.models import JournalEntry
from gateway.payments.ledger.service import PostingLine, ensure_account

CURRENCY_EXPONENTS: dict[str, int] = {"EUR": 2, "INR": 2, "JPY": 0, "USD": 2}
FX_CLEARING_KIND = "fx_clearing"

DEMO_RATES_VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)
DEMO_RATES_PER_USD: dict[str, Decimal] = {
    "EUR": Decimal("0.92"),
    "INR": Decimal("83.25"),
    "JPY": Decimal("151.5"),
    "USD": Decimal(1),
}
_RATE_QUANTUM = Decimal("1E-10")
_PRECISION = 60


class FxRateNotFoundError(Exception):
    pass


class FxAmountTooSmallError(Exception):
    pass


@dataclass(frozen=True)
class FxQuote:
    rate_id: str
    source_currency: str
    target_currency: str
    rate: Decimal
    source_minor: int
    target_minor: int


def convert_minor(amount_minor: int, rate: Decimal, source_currency: str, target_currency: str) -> int:
    """`floor(amount * rate * 10**(exp_target - exp_source))` in Decimal. 0 is returned, not raised."""
    shift = CURRENCY_EXPONENTS[target_currency] - CURRENCY_EXPONENTS[source_currency]
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        value = Decimal(amount_minor) * rate * Decimal(10) ** shift
        return int(value.to_integral_value(rounding=ROUND_FLOOR))


def quote(
    session: Session, source_currency: str, target_currency: str, amount_minor: int, now: datetime
) -> FxQuote:
    """Raises ValueError (same currency), FxRateNotFoundError, FxAmountTooSmallError. Reads only."""
    if source_currency == target_currency:
        raise ValueError("an FX quote needs two different currencies")
    row = session.execute(
        select(FxRate.id, FxRate.rate)
        .where(
            FxRate.base_currency == source_currency,
            FxRate.quote_currency == target_currency,
            FxRate.valid_from <= now,
        )
        .order_by(FxRate.valid_from.desc())
        .limit(1)
    ).one_or_none()
    if row is None:
        raise FxRateNotFoundError(f"no {source_currency}->{target_currency} rate in force")
    target_minor = convert_minor(amount_minor, row.rate, source_currency, target_currency)
    if target_minor <= 0:
        raise FxAmountTooSmallError(f"{amount_minor} {source_currency} converts to 0 {target_currency}")
    return FxQuote(
        rate_id=row.id,
        source_currency=source_currency,
        target_currency=target_currency,
        rate=row.rate,
        source_minor=amount_minor,
        target_minor=target_minor,
    )


def fx_lines(
    session: Session, fx_quote: FxQuote, debit_account_id: uuid.UUID, credit_account_id: uuid.UUID
) -> list[PostingLine]:
    """Four lines; each currency nets to zero through its platform `fx_clearing` account."""
    source, target = fx_quote.source_currency, fx_quote.target_currency
    source_clearing = ensure_account(session, "platform", None, FX_CLEARING_KIND, source)
    target_clearing = ensure_account(session, "platform", None, FX_CLEARING_KIND, target)
    return [
        PostingLine(debit_account_id, source, fx_quote.source_minor),
        PostingLine(source_clearing, source, -fx_quote.source_minor),
        PostingLine(target_clearing, target, fx_quote.target_minor),
        PostingLine(credit_account_id, target, -fx_quote.target_minor),
    ]


def journal_fx_rate(session: Session, ref_type: str, ref_id: uuid.UUID) -> FxRate | None:
    """The rate an FX journal was posted at; None when that journal had no FX (or does not exist)."""
    return session.execute(
        select(FxRate)
        .join(JournalEntry, JournalEntry.fx_rate_id == FxRate.id)
        .where(JournalEntry.ref_type == ref_type, JournalEntry.ref_id == ref_id)
        .limit(1)
    ).scalar_one_or_none()


def reversal_lines(
    session: Session,
    rate: FxRate,
    already_reversed_minor: int,
    amount_minor: int,
    debit_account_id: uuid.UUID,
    credit_account_id: uuid.UUID,
) -> list[PostingLine]:
    """Reverses `amount_minor` more of the rate's base currency at that same rate, after
    `already_reversed_minor` was reversed before: credits `credit_account_id` (base currency) by
    `amount_minor` and debits `debit_account_id` (quote currency) by
    `convert(already + amount) - convert(already)`. Converting cumulative totals means splitting a reversal
    into tiny pieces cannot round the debit away, and the debits sum to exactly the original conversion
    once everything is reversed. A quote-currency leg of 0 is left out (each currency still nets to zero)."""
    before = convert_minor(already_reversed_minor, rate.rate, rate.base_currency, rate.quote_currency)
    after = convert_minor(
        already_reversed_minor + amount_minor, rate.rate, rate.base_currency, rate.quote_currency
    )
    reverse = FxQuote(
        rate_id=rate.id,
        source_currency=rate.quote_currency,
        target_currency=rate.base_currency,
        rate=rate.rate,
        source_minor=after - before,
        target_minor=amount_minor,
    )
    lines = fx_lines(session, reverse, debit_account_id, credit_account_id)
    return [line for line in lines if line.amount_minor != 0]


def demo_rates() -> list[FxRate]:
    """The 12 fixed demo rates the migration seeds (cross rates derived from the USD rates, 10 decimals)."""
    rates: list[FxRate] = []
    for base, base_per_usd in sorted(DEMO_RATES_PER_USD.items()):
        for quote_currency, quote_per_usd in sorted(DEMO_RATES_PER_USD.items()):
            if base == quote_currency:
                continue
            with localcontext() as ctx:
                ctx.prec = _PRECISION
                rate = (quote_per_usd / base_per_usd).quantize(_RATE_QUANTUM, rounding=ROUND_HALF_EVEN)
            rates.append(
                FxRate(
                    id=f"demo-{base}{quote_currency}-v1",
                    base_currency=base,
                    quote_currency=quote_currency,
                    rate=rate,
                    valid_from=DEMO_RATES_VALID_FROM,
                )
            )
    return rates


def seed_demo_rates(session: Session) -> None:
    """Inserts the demo rates that are missing. No commit."""
    for rate in demo_rates():
        session.execute(
            insert(FxRate)
            .values(
                id=rate.id,
                base_currency=rate.base_currency,
                quote_currency=rate.quote_currency,
                rate=rate.rate,
                valid_from=rate.valid_from,
            )
            .on_conflict_do_nothing()
        )
