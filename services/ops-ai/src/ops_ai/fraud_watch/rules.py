from sqlalchemy import ColumnElement, case

from ops_ai.ingest.models import PaymentFact

MICRO_MAX_UNITS = 1
CURRENCY_EXPONENT = {"JPY": 0}
DEFAULT_CURRENCY_EXPONENT = 2
EPISODE_GAP_SECONDS = 120
MIN_MICRO_PAYMENTS = 10
MIN_DISTINCT_CARDS = 3
MIN_MICRO_SHARE = 0.8
HIGH_DECLINE_RATIO = 0.5
LIVE_LOOKBACK_MINUTES = 30
MAX_ANALYZED_MICRO_PAYMENTS = 5000


def is_micro(amount_minor: int, currency: str) -> bool:
    """True when the amount is at most MICRO_MAX_UNITS whole units of the currency."""
    exponent = CURRENCY_EXPONENT.get(currency, DEFAULT_CURRENCY_EXPONENT)
    limit: int = MICRO_MAX_UNITS * 10**exponent
    return amount_minor <= limit


def micro_condition() -> ColumnElement[bool]:
    """`is_micro` as a SQL condition on payment_facts, so only micro payments are read."""
    limit = case(
        {currency: MICRO_MAX_UNITS * 10**exponent for currency, exponent in CURRENCY_EXPONENT.items()},
        value=PaymentFact.currency,
        else_=MICRO_MAX_UNITS * 10**DEFAULT_CURRENCY_EXPONENT,
    )
    return PaymentFact.amount_minor <= limit
