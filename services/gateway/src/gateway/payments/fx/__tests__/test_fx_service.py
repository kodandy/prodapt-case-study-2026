"""Tests for the FX service (`convert_minor`, `quote`, `fx_lines`) and `post_journal(fx_rate_id=...)`,
written from docs/specs/gateway-basic.md section "B1.4 / B2.3 / B2.5 / P0-2 / P0-3" (Decisions 2-4).

Needs the 12 migration-seeded demo rates (`gateway.fx_rates`) to survive the per-test table reset.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from gateway.clock import FixedClock
from gateway.payments.fx.service import (
    CURRENCY_EXPONENTS,
    FX_CLEARING_KIND,
    FxAmountTooSmallError,
    FxQuote,
    FxRateNotFoundError,
    convert_minor,
    fx_lines,
    quote,
)
from gateway.payments.ledger.service import PostingLine, ensure_account, post_journal
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

CURRENCIES = ("EUR", "INR", "JPY", "USD")
SEED_VALID_FROM = datetime(2026, 1, 1, tzinfo=UTC)
USD_RATES = {"EUR": Decimal("0.92"), "JPY": Decimal("151.5"), "INR": Decimal("83.25")}
HALF_ULP = Decimal("0.00000000005")  # rates are rounded to 10 decimals; either rounding mode is within this


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _insert_rate(
    session_factory: sessionmaker[Session],
    rate_id: str,
    base: str,
    quote_currency: str,
    rate: str,
    valid_from: datetime,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO gateway.fx_rates (id, base_currency, quote_currency, rate, valid_from) "
                "VALUES (:id, :base, :quote, :rate, :valid_from)"
            ),
            {
                "id": rate_id,
                "base": base,
                "quote": quote_currency,
                "rate": Decimal(rate),
                "valid_from": valid_from,
            },
        )


# --- constants -----------------------------------------------------------------------------------------


def test_currency_exponents_are_zero_for_jpy_and_two_for_the_others() -> None:
    # Arrange / Act
    exponents = dict(CURRENCY_EXPONENTS)

    # Assert
    assert exponents == {"USD": 2, "EUR": 2, "JPY": 0, "INR": 2}


def test_fx_clearing_kind_is_fx_clearing() -> None:
    # Arrange / Act / Assert
    assert FX_CLEARING_KIND == "fx_clearing"


# --- convert_minor -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount_minor", "rate", "source", "target", "expected"),
    [
        pytest.param(10_000, Decimal("0.92"), "USD", "EUR", 9_200, id="usd-to-eur-same-exponent"),
        pytest.param(10_000, Decimal("83.25"), "USD", "INR", 832_500, id="usd-to-inr-same-exponent"),
        pytest.param(10_000, Decimal("151.5"), "USD", "JPY", 15_150, id="usd-to-jpy-exponent-drops-by-two"),
        pytest.param(
            1_000, Decimal("0.0066006600"), "JPY", "USD", 660, id="jpy-to-usd-exponent-rises-by-two"
        ),
        pytest.param(15, Decimal("0.92"), "USD", "EUR", 13, id="floor-not-round-13.8"),
        pytest.param(1_501, Decimal("0.92"), "USD", "EUR", 1_380, id="floor-1380.92"),
        pytest.param(1_500, Decimal("151.5"), "USD", "JPY", 2_272, id="floor-2272.5-to-jpy"),
        pytest.param(1, Decimal("0.92"), "USD", "EUR", 0, id="result-zero-is-returned-not-raised"),
        pytest.param(1, Decimal("0.0066006600"), "JPY", "USD", 0, id="one-jpy-is-zero-usd-minor"),
        pytest.param(100, Decimal("1.5"), "EUR", "INR", 150, id="same-exponent-pair-eur-inr"),
    ],
)
def test_convert_minor_applies_rate_and_exponent_shift_with_floor(
    amount_minor: int, rate: Decimal, source: str, target: str, expected: int
) -> None:
    # Arrange / Act
    result = convert_minor(amount_minor, rate, source, target)

    # Assert
    assert result == expected


def test_convert_minor_is_exact_beyond_float_precision() -> None:
    # Arrange: 2**53 + 1 is not representable as a float
    amount_minor = 9_007_199_254_740_993

    # Act
    result = convert_minor(amount_minor, Decimal("1"), "USD", "EUR")

    # Assert
    assert result == 9_007_199_254_740_993


def test_convert_minor_handles_the_largest_request_amount_to_jpy() -> None:
    # Arrange: 10**12 USD minor units at 151.5 JPY per USD, JPY has no minor digits
    amount_minor = 10**12

    # Act
    result = convert_minor(amount_minor, Decimal("151.5"), "USD", "JPY")

    # Assert
    assert result == 1_515_000_000_000


def test_convert_minor_returns_an_int() -> None:
    # Arrange / Act
    result = convert_minor(10_000, Decimal("0.92"), "USD", "EUR")

    # Assert
    assert type(result) is int


# --- seeded rates (Decision 2) -------------------------------------------------------------------------


def test_seeded_rates_cover_all_twelve_ordered_pairs_with_the_documented_ids(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    expected_ids = {
        f"demo-{base}{quoted}-v1" for base in CURRENCIES for quoted in CURRENCIES if base != quoted
    }

    # Act
    rows = _rows(
        session_factory,
        "SELECT id, base_currency, quote_currency, valid_from FROM gateway.fx_rates ORDER BY id",
    )

    # Assert
    assert {str(row["id"]) for row in rows} == expected_ids
    assert len(rows) == 12
    assert {(str(row["base_currency"]), str(row["quote_currency"])) for row in rows} == {
        (base, quoted) for base in CURRENCIES for quoted in CURRENCIES if base != quoted
    }
    assert {row["valid_from"] for row in rows} == {SEED_VALID_FROM}


@pytest.mark.parametrize(("target", "expected"), sorted(USD_RATES.items()))
def test_seeded_usd_rates_are_the_documented_demo_rates(
    session_factory: sessionmaker[Session], target: str, expected: Decimal
) -> None:
    # Arrange / Act
    rows = _rows(
        session_factory,
        "SELECT rate FROM gateway.fx_rates WHERE id = :id",
        id=f"demo-USD{target}-v1",
    )

    # Assert
    assert [row["rate"] for row in rows] == [expected]


@pytest.mark.parametrize(
    ("base", "target"),
    [(b, t) for b in CURRENCIES for t in CURRENCIES if b != t and "USD" not in (b, t)],
)
def test_seeded_cross_rates_are_derived_from_the_usd_rates_rounded_to_ten_decimals(
    session_factory: sessionmaker[Session], base: str, target: str
) -> None:
    # Arrange
    exact = USD_RATES[target] / USD_RATES[base]

    # Act
    rows = _rows(
        session_factory, "SELECT rate FROM gateway.fx_rates WHERE id = :id", id=f"demo-{base}{target}-v1"
    )

    # Assert
    assert len(rows) == 1
    rate = Decimal(str(rows[0]["rate"]))
    assert abs(rate - exact) <= HALF_ULP


@pytest.mark.parametrize("base", ["EUR", "JPY", "INR"])
def test_seeded_rate_into_usd_is_the_inverse_of_the_usd_rate_within_rounding(
    session_factory: sessionmaker[Session], base: str
) -> None:
    # Arrange
    exact = Decimal(1) / USD_RATES[base]

    # Act
    rows = _rows(session_factory, "SELECT rate FROM gateway.fx_rates WHERE id = :id", id=f"demo-{base}USD-v1")

    # Assert
    assert len(rows) == 1
    assert abs(Decimal(str(rows[0]["rate"])) - exact) <= HALF_ULP


def test_fx_rates_reject_a_non_positive_rate(session_factory: sessionmaker[Session]) -> None:
    # Arrange / Act / Assert
    with pytest.raises(IntegrityError):
        _insert_rate(session_factory, "bad-zero", "USD", "EUR", "0", datetime(2026, 2, 1, tzinfo=UTC))


def test_fx_rates_reject_a_second_rate_for_the_same_pair_and_valid_from(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(IntegrityError):
        _insert_rate(session_factory, "dup-usdeur", "USD", "EUR", "0.90", SEED_VALID_FROM)


# --- quote ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target", "expected_rate", "expected_minor"),
    [
        pytest.param("EUR", Decimal("0.92"), 9_200, id="usd-eur"),
        pytest.param("JPY", Decimal("151.5"), 15_150, id="usd-jpy"),
        pytest.param("INR", Decimal("83.25"), 8_325 * 100, id="usd-inr"),
    ],
)
def test_quote_uses_the_seeded_usd_rate_and_converts(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    target: str,
    expected_rate: Decimal,
    expected_minor: int,
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = quote(session, "USD", target, 10_000, clock())

    # Assert
    assert result == FxQuote(
        rate_id=f"demo-USD{target}-v1",
        source_currency="USD",
        target_currency=target,
        rate=expected_rate,
        source_minor=10_000,
        target_minor=expected_minor,
    )


def test_quote_floors_the_converted_amount(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = quote(session, "USD", "EUR", 1_501, clock())

    # Assert
    assert (result.source_minor, result.target_minor) == (1_501, 1_380)


def test_quote_from_jpy_returns_the_amount_in_the_target_minor_unit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        rate = Decimal(
            str(
                session.execute(
                    text("SELECT rate FROM gateway.fx_rates WHERE id = 'demo-JPYUSD-v1'")
                ).scalar_one()
            )
        )

        # Act
        result = quote(session, "JPY", "USD", 100_000, clock())

    # Assert
    assert result.rate == rate
    assert result.rate_id == "demo-JPYUSD-v1"
    floor_of_rate_product = (Decimal(100_000) * rate * 100).to_integral_value(rounding="ROUND_FLOOR")
    assert result.target_minor == int(floor_of_rate_product)
    assert result.target_minor == 66_006


def test_quote_raises_value_error_for_the_same_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session, pytest.raises(ValueError):
        # Act
        quote(session, "USD", "USD", 10_000, clock())


def test_quote_raises_amount_too_small_when_the_converted_amount_is_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session, pytest.raises(FxAmountTooSmallError):
        # Act
        quote(session, "USD", "EUR", 1, clock())


def test_quote_raises_amount_too_small_for_one_jpy_into_usd(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session, pytest.raises(FxAmountTooSmallError):
        # Act
        quote(session, "JPY", "USD", 1, clock())


def test_quote_raises_rate_not_found_before_the_first_rate_is_valid(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    before_seed = datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC)
    with session_factory() as session, pytest.raises(FxRateNotFoundError):
        # Act
        quote(session, "USD", "EUR", 10_000, before_seed)


def test_quote_uses_the_seed_rate_exactly_at_its_valid_from(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = quote(session, "USD", "EUR", 10_000, SEED_VALID_FROM)

    # Assert
    assert result.rate_id == "demo-USDEUR-v1"
    assert result.target_minor == 9_200


def test_quote_picks_the_latest_rate_whose_valid_from_is_not_after_now(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_rate(session_factory, "test-USDEUR-v2", "USD", "EUR", "0.90", datetime(2026, 6, 1, tzinfo=UTC))
    _insert_rate(session_factory, "test-USDEUR-v3", "USD", "EUR", "0.80", datetime(2027, 1, 1, tzinfo=UTC))
    before_v2 = datetime(2026, 5, 31, 23, 59, 59, tzinfo=UTC)
    at_v2 = datetime(2026, 6, 1, tzinfo=UTC)
    after_v2 = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)

    # Act
    with session_factory() as session:
        quotes = [quote(session, "USD", "EUR", 10_000, now) for now in (before_v2, at_v2, after_v2)]

    # Assert
    assert [(q.rate_id, q.rate, q.target_minor) for q in quotes] == [
        ("demo-USDEUR-v1", Decimal("0.92"), 9_200),
        ("test-USDEUR-v2", Decimal("0.90"), 9_000),
        ("test-USDEUR-v2", Decimal("0.90"), 9_000),
    ]


def test_quote_only_considers_the_requested_ordered_pair(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_rate(session_factory, "test-EURUSD-v2", "EUR", "USD", "2", datetime(2026, 6, 1, tzinfo=UTC))

    # Act
    with session_factory() as session:
        usd_eur = quote(session, "USD", "EUR", 10_000, clock())
        eur_usd = quote(session, "EUR", "USD", 10_000, clock())

    # Assert
    assert (usd_eur.rate_id, usd_eur.target_minor) == ("demo-USDEUR-v1", 9_200)
    assert (eur_usd.rate_id, eur_usd.target_minor) == ("test-EURUSD-v2", 20_000)


def test_quote_does_not_write_anything(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        quote(session, "USD", "EUR", 10_000, clock())

    # Assert
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.journal_entries") == [{"n": 0}]
    assert _rows(session_factory, "SELECT count(*) AS n FROM gateway.fx_rates") == [{"n": 12}]


# --- fx_lines ------------------------------------------------------------------------------------------


def test_fx_lines_returns_four_lines_through_the_fx_clearing_account_of_each_currency(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        debit = ensure_account(session, "user", uuid.uuid4(), "wallet", "USD")
        credit = ensure_account(session, "user", uuid.uuid4(), "wallet", "EUR")
        fx_quote = quote(session, "USD", "EUR", 10_000, clock())

        # Act
        lines = fx_lines(session, fx_quote, debit, credit)
        clearing_usd = ensure_account(session, "platform", None, "fx_clearing", "USD")
        clearing_eur = ensure_account(session, "platform", None, "fx_clearing", "EUR")
        session.commit()

    # Assert
    assert len(lines) == 4
    assert set(lines) == {
        PostingLine(account_id=debit, currency="USD", amount_minor=10_000),
        PostingLine(account_id=clearing_usd, currency="USD", amount_minor=-10_000),
        PostingLine(account_id=clearing_eur, currency="EUR", amount_minor=9_200),
        PostingLine(account_id=credit, currency="EUR", amount_minor=-9_200),
    }
    assert clearing_usd != clearing_eur


def test_fx_lines_sum_to_zero_per_currency(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    with session_factory() as session:
        debit = ensure_account(session, "user", uuid.uuid4(), "wallet", "USD")
        credit = ensure_account(session, "user", uuid.uuid4(), "wallet", "JPY")
        fx_quote = quote(session, "USD", "JPY", 1_501, clock())

        # Act
        lines = fx_lines(session, fx_quote, debit, credit)

    # Assert
    totals: dict[str, int] = {}
    for line in lines:
        totals[line.currency] = totals.get(line.currency, 0) + line.amount_minor
    assert totals == {"USD": 0, "JPY": 0}
    assert sorted((line.currency, line.amount_minor) for line in lines) == [
        ("JPY", -2_274),
        ("JPY", 2_274),
        ("USD", -1_501),
        ("USD", 1_501),
    ]


def test_fx_clearing_accounts_are_platform_accounts_without_a_balance_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        debit = ensure_account(session, "user", uuid.uuid4(), "wallet", "USD")
        credit = ensure_account(session, "user", uuid.uuid4(), "wallet", "EUR")
        fx_quote = quote(session, "USD", "EUR", 10_000, clock())

        # Act
        fx_lines(session, fx_quote, debit, credit)
        session.commit()

    # Assert
    clearing = _rows(
        session_factory,
        "SELECT a.owner_type, a.owner_id, a.kind, a.currency, "
        "(SELECT count(*) FROM gateway.account_balances b WHERE b.account_id = a.id) AS balance_rows "
        "FROM gateway.ledger_accounts a WHERE a.kind = 'fx_clearing' ORDER BY a.currency",
    )
    platform_clearing = {"owner_type": "platform", "owner_id": None, "kind": "fx_clearing"}
    assert clearing == [
        {**platform_clearing, "currency": "EUR", "balance_rows": 0},
        {**platform_clearing, "currency": "USD", "balance_rows": 0},
    ]


# --- post_journal(fx_rate_id=...) ----------------------------------------------------------------------


def test_post_journal_stores_the_fx_rate_id_on_the_journal_entry(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    ref_id = uuid.uuid4()
    with session_factory() as session:
        debit = ensure_account(session, "user", uuid.uuid4(), "wallet", "USD")
        credit = ensure_account(session, "user", uuid.uuid4(), "wallet", "EUR")
        fx_quote = quote(session, "USD", "EUR", 10_000, clock())
        lines = fx_lines(session, fx_quote, debit, credit)

        # Act
        journal_id = post_journal(session, clock, "fx_test", ref_id, lines, fx_rate_id=fx_quote.rate_id)
        session.commit()

    # Assert
    journals = _rows(
        session_factory,
        "SELECT id, ref_type, ref_id, fx_rate_id FROM gateway.journal_entries",
    )
    postings = _rows(
        session_factory,
        "SELECT currency, sum(amount_minor)::bigint AS total, count(*) AS n FROM gateway.postings "
        "WHERE journal_id = :j GROUP BY currency ORDER BY currency",
        j=journal_id,
    )
    assert journals == [
        {"id": journal_id, "ref_type": "fx_test", "ref_id": ref_id, "fx_rate_id": "demo-USDEUR-v1"}
    ]
    assert postings == [{"currency": "EUR", "total": 0, "n": 2}, {"currency": "USD", "total": 0, "n": 2}]


def test_post_journal_leaves_fx_rate_id_null_by_default(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        debit = ensure_account(session, "platform", None, "provider_receivable", "USD")
        credit = ensure_account(session, "platform", None, "merchant_payable", "USD")
        lines = [
            PostingLine(account_id=debit, currency="USD", amount_minor=100),
            PostingLine(account_id=credit, currency="USD", amount_minor=-100),
        ]

        # Act
        post_journal(session, clock, "payment", uuid.uuid4(), lines)
        session.commit()

    # Assert
    assert _rows(session_factory, "SELECT fx_rate_id FROM gateway.journal_entries") == [{"fx_rate_id": None}]


def test_post_journal_rejects_an_fx_rate_id_that_is_not_in_fx_rates(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        debit = ensure_account(session, "platform", None, "provider_receivable", "USD")
        credit = ensure_account(session, "platform", None, "merchant_payable", "USD")
        lines = [
            PostingLine(account_id=debit, currency="USD", amount_minor=100),
            PostingLine(account_id=credit, currency="USD", amount_minor=-100),
        ]

        # Act / Assert
        with pytest.raises(IntegrityError):
            post_journal(session, clock, "payment", uuid.uuid4(), lines, fx_rate_id="no-such-rate")
        session.rollback()
