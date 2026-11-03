import uuid
from collections import defaultdict

import pytest
from gateway.clock import FixedClock
from gateway.payments.ledger.service import (
    PostingLine,
    UnbalancedJournalError,
    ensure_account,
    post_journal,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

CURRENCIES = ["USD", "EUR", "JPY"]
NONZERO_AMOUNTS = st.integers(min_value=-(10**9), max_value=10**9).filter(lambda value: value != 0)
AMOUNTS_PER_CURRENCY = st.dictionaries(
    st.sampled_from(CURRENCIES),
    st.lists(NONZERO_AMOUNTS, min_size=1, max_size=4),
    min_size=1,
    max_size=3,
)
PROPERTY_SETTINGS = settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _balanced_spec(amounts_per_currency: dict[str, list[int]]) -> list[tuple[str, int]]:
    """Lines (currency, amount) where each currency gets a closing line so it sums to zero."""
    spec: list[tuple[str, int]] = []
    for currency, amounts in sorted(amounts_per_currency.items()):
        spec.extend((currency, amount) for amount in amounts)
        residual = sum(amounts)
        if residual != 0:
            spec.append((currency, -residual))
    return spec


def _lines(session: Session, spec: list[tuple[str, int]]) -> list[PostingLine]:
    return [
        PostingLine(
            account_id=ensure_account(session, "platform", None, f"prop_{index}", currency),
            currency=currency,
            amount_minor=amount,
        )
        for index, (currency, amount) in enumerate(spec)
    ]


def test_post_journal_stores_a_balanced_journal_with_its_postings(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    ref_id = uuid.uuid4()
    with session_factory() as session:
        debit = ensure_account(session, "platform", None, "provider_receivable", "USD")
        credit = ensure_account(session, "merchant", uuid.uuid4(), "merchant_payable", "USD")
        lines = [
            PostingLine(account_id=debit, currency="USD", amount_minor=1500),
            PostingLine(account_id=credit, currency="USD", amount_minor=-1500),
        ]

        # Act
        journal_id = post_journal(session, clock, "payment", ref_id, lines)
        session.commit()

    # Assert
    journals = _rows(session_factory, "SELECT id, ref_type, ref_id, created_at FROM gateway.journal_entries")
    postings = _rows(
        session_factory,
        "SELECT account_id, currency, amount_minor FROM gateway.postings WHERE journal_id = :journal_id "
        "ORDER BY amount_minor",
        journal_id=journal_id,
    )
    assert journals == [{"id": journal_id, "ref_type": "payment", "ref_id": ref_id, "created_at": clock()}]
    assert postings == [
        {"account_id": credit, "currency": "USD", "amount_minor": -1500},
        {"account_id": debit, "currency": "USD", "amount_minor": 1500},
    ]


def test_post_journal_rejects_a_journal_that_does_not_sum_to_zero(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    ref_id = uuid.uuid4()
    with session_factory() as session:
        debit = ensure_account(session, "platform", None, "provider_receivable", "USD")
        credit = ensure_account(session, "platform", None, "merchant_payable", "USD")
        lines = [
            PostingLine(account_id=debit, currency="USD", amount_minor=100),
            PostingLine(account_id=credit, currency="USD", amount_minor=-99),
        ]

        # Act
        with pytest.raises(UnbalancedJournalError):
            post_journal(session, clock, "payment", ref_id, lines)
        session.rollback()

    # Assert
    journals = _rows(session_factory, "SELECT id FROM gateway.journal_entries WHERE ref_id = :r", r=ref_id)
    assert journals == []


def test_post_journal_rejects_amounts_that_cancel_only_across_currencies(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        usd = ensure_account(session, "platform", None, "provider_receivable", "USD")
        eur = ensure_account(session, "platform", None, "provider_receivable", "EUR")
        lines = [
            PostingLine(account_id=usd, currency="USD", amount_minor=100),
            PostingLine(account_id=eur, currency="EUR", amount_minor=-100),
        ]

        # Act / Assert
        with pytest.raises(UnbalancedJournalError):
            post_journal(session, clock, "fx", uuid.uuid4(), lines)


def test_post_journal_accepts_two_currencies_that_each_balance(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        accounts = {
            (currency, side): ensure_account(session, "platform", None, f"fx_{side}", currency)
            for currency in ("USD", "EUR")
            for side in ("a", "b")
        }
        lines = [
            PostingLine(account_id=accounts[("USD", "a")], currency="USD", amount_minor=100),
            PostingLine(account_id=accounts[("USD", "b")], currency="USD", amount_minor=-100),
            PostingLine(account_id=accounts[("EUR", "a")], currency="EUR", amount_minor=90),
            PostingLine(account_id=accounts[("EUR", "b")], currency="EUR", amount_minor=-90),
        ]

        # Act
        journal_id = post_journal(session, clock, "fx", uuid.uuid4(), lines)
        session.commit()

    # Assert
    postings = _rows(
        session_factory,
        "SELECT currency, sum(amount_minor)::bigint AS total, count(*) AS n FROM gateway.postings "
        "WHERE journal_id = :j GROUP BY currency ORDER BY currency",
        j=journal_id,
    )
    assert postings == [
        {"currency": "EUR", "total": 0, "n": 2},
        {"currency": "USD", "total": 0, "n": 2},
    ]


@PROPERTY_SETTINGS
@given(amounts_per_currency=AMOUNTS_PER_CURRENCY)
def test_property_any_balanced_multi_currency_journal_is_accepted_and_stored_balanced(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    amounts_per_currency: dict[str, list[int]],
) -> None:
    # Arrange
    spec = _balanced_spec(amounts_per_currency)
    ref_id = uuid.uuid4()

    # Act
    with session_factory() as session:
        journal_id = post_journal(session, clock, "property", ref_id, _lines(session, spec))
        session.commit()

    # Assert
    stored = _rows(
        session_factory,
        "SELECT currency, amount_minor FROM gateway.postings WHERE journal_id = :j",
        j=journal_id,
    )
    totals: dict[object, int] = defaultdict(int)
    for row in stored:
        totals[row["currency"]] += int(row["amount_minor"])  # type: ignore[call-overload]
    assert dict(totals) == dict.fromkeys(amounts_per_currency, 0)
    assert sorted((str(row["currency"]), row["amount_minor"]) for row in stored) == sorted(spec)


@PROPERTY_SETTINGS
@given(amounts_per_currency=AMOUNTS_PER_CURRENCY, delta=st.integers(min_value=1, max_value=10**6))
def test_property_any_journal_with_a_nonzero_residual_is_rejected_and_not_stored(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    amounts_per_currency: dict[str, list[int]],
    delta: int,
) -> None:
    # Arrange
    spec = _balanced_spec(amounts_per_currency)
    currency, amount = spec[0]
    spec[0] = (currency, amount + delta)
    ref_id = uuid.uuid4()

    # Act
    with session_factory() as session:
        lines = _lines(session, spec)
        with pytest.raises(UnbalancedJournalError):
            post_journal(session, clock, "property", ref_id, lines)
        session.rollback()

    # Assert
    assert _rows(session_factory, "SELECT id FROM gateway.journal_entries WHERE ref_id = :r", r=ref_id) == []


def test_ensure_account_returns_the_same_id_on_repeat_calls(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    owner_id = uuid.uuid4()

    # Act
    with session_factory() as session:
        first = ensure_account(session, "merchant", owner_id, "merchant_payable", "USD")
        second = ensure_account(session, "merchant", owner_id, "merchant_payable", "USD")
        session.commit()

    # Assert
    rows = _rows(session_factory, "SELECT id FROM gateway.ledger_accounts WHERE owner_id = :o", o=owner_id)
    assert first == second
    assert rows == [{"id": first}]


def test_ensure_account_returns_the_same_id_for_a_platform_account_without_owner(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    with session_factory() as session:
        first = ensure_account(session, "platform", None, "provider_receivable", "USD")
        session.commit()
    with session_factory() as session:
        second = ensure_account(session, "platform", None, "provider_receivable", "USD")
        session.commit()

    # Assert
    rows = _rows(
        session_factory,
        "SELECT id FROM gateway.ledger_accounts WHERE owner_type = 'platform' AND owner_id IS NULL",
    )
    assert first == second
    assert rows == [{"id": first}]


def test_ensure_account_creates_distinct_accounts_per_currency_and_kind(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    with session_factory() as session:
        usd = ensure_account(session, "platform", None, "provider_receivable", "USD")
        eur = ensure_account(session, "platform", None, "provider_receivable", "EUR")
        fees = ensure_account(session, "platform", None, "fees", "USD")
        session.commit()

    # Assert
    assert len({usd, eur, fees}) == 3
