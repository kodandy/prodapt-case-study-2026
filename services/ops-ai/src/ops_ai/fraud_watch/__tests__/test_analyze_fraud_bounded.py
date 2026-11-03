"""Tests for the bounded analyze_fraud, written from docs/specs/sq3.md H-2 (auditor H1).

The limit constant is patched with monkeypatch on whichever fraud_watch module defines it, so the
implementation must read MAX_ANALYZED_MICRO_PAYMENTS at call time (not copy it into a default argument).
"""

import dataclasses
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import ModuleType

import pytest
from ops_ai.fraud_watch import CardTestingEpisode, FraudAnalysis, analyze_fraud
from ops_ai.fraud_watch.rules import CURRENCY_EXPONENT, is_micro
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from sqlalchemy import event, select, text
from sqlalchemy.engine import Connection, Engine, ExecutionContext
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.orm import Session, sessionmaker

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
WINDOW_START = BASE - timedelta(hours=1)
WINDOW_END = BASE + timedelta(days=1)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
LIMIT_NAME = "MAX_ANALYZED_MICRO_PAYMENTS"
SMALL_LIMIT = 20
SPEC_LIMIT = 5000
LIMIT_MODULES = ("ops_ai.fraud_watch.analysis", "ops_ai.fraud_watch.rules", "ops_ai.fraud_watch")

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :method, 'declined', 'acq_a', NULL, '400000', "
    ":card_ref, 1, :occurred_at)"
)
INSERT_SERIES = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) "
    "SELECT CAST('00000000-0000-4000-8000-' || lpad(to_hex(CAST(:first_n AS bigint) + g), 12, '0') AS uuid), "
    ":merchant_id, :amount_minor, 'USD', 'card', 'declined', 'acq_a', NULL, '400000', "
    "lpad(to_hex(g % 4 + 1), 32, '0'), 1, CAST(:start AS timestamptz) + g * interval '1 second' "
    "FROM generate_series(0, CAST(:count AS integer) - 1) AS g"
)


def _series_id(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-4000-8000-{n:012x}")


def _limit_modules() -> list[ModuleType]:
    return [module for module in map(import_module, LIMIT_MODULES) if LIMIT_NAME in vars(module)]


def _set_limit(monkeypatch: pytest.MonkeyPatch, value: int) -> None:
    modules = _limit_modules()
    if not modules:
        pytest.fail(f"{LIMIT_NAME} is not defined in any of {LIMIT_MODULES}")
    for module in modules:
        monkeypatch.setattr(module, LIMIT_NAME, value)


def _insert_series(
    session_factory: sessionmaker[Session],
    count: int,
    *,
    first_n: int = 1,
    start: datetime = BASE,
    amount_minor: int = 50,
    merchant_id: uuid.UUID = MERCHANT,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_SERIES,
            {
                "first_n": first_n,
                "count": count,
                "start": start,
                "amount_minor": amount_minor,
                "merchant_id": merchant_id,
            },
        )


def _fact(
    session: Session,
    n: int,
    at: datetime,
    *,
    amount_minor: int = 50,
    currency: str = "USD",
    method: str = "card",
    merchant_id: uuid.UUID = MERCHANT,
    card_ref: str | None = None,
) -> uuid.UUID:
    payment_id = uuid.UUID(int=n)
    session.execute(
        INSERT_FACT,
        {
            "payment_id": payment_id,
            "merchant_id": merchant_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "method": method,
            "card_ref": card_ref if card_ref is not None else f"{n % 4 + 1:032x}",
            "occurred_at": at,
        },
    )
    return payment_id


def _micro_burst(
    session_factory: sessionmaker[Session],
    count: int,
    *,
    first_n: int = 1,
    start: datetime = BASE,
    step_seconds: int = 10,
) -> list[uuid.UUID]:
    with session_factory.begin() as session:
        return [
            _fact(session, first_n + index, start + timedelta(seconds=index * step_seconds))
            for index in range(count)
        ]


def _analyze(session_factory: sessionmaker[Session]) -> FraudAnalysis:
    with session_factory() as session:
        return analyze_fraud(session, SCOPE, WINDOW_START, WINDOW_END)


def _only_episode(analysis: FraudAnalysis) -> CardTestingEpisode:
    assert len(analysis.episodes) == 1
    return analysis.episodes[0]


@dataclasses.dataclass
class _Statements:
    row_counts: list[int] = dataclasses.field(default_factory=list)


@contextmanager
def _recording(engine: Engine) -> Iterator[_Statements]:
    recorded = _Statements()

    def _after(
        _conn: Connection,
        cursor: DBAPICursor,
        _statement: str,
        _parameters: object,
        _context: ExecutionContext | None,
        _executemany: bool,
    ) -> None:
        recorded.row_counts.append(int(cursor.rowcount))

    event.listen(engine, "after_cursor_execute", _after)
    try:
        yield recorded
    finally:
        event.remove(engine, "after_cursor_execute", _after)


# --- the limit constant and the new field -----------------------------------------------------------------


def test_the_analysis_limit_is_5000_micro_payments() -> None:
    # Arrange / Act
    values = {vars(module)[LIMIT_NAME] for module in _limit_modules()}

    # Assert
    assert values == {SPEC_LIMIT}


def test_fraud_analysis_has_truncated_as_its_last_field_defaulting_to_false() -> None:
    # Arrange / Act
    fields = dataclasses.fields(FraudAnalysis)

    # Assert
    assert [field.name for field in fields] == ["start", "end", "micro_payments", "episodes", "truncated"]
    assert fields[-1].default is False


# --- results under the limit are unchanged ------------------------------------------------------------------


def test_a_burst_under_the_limit_is_reported_in_full_and_is_not_truncated(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _micro_burst(session_factory, 12)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is False
    assert analysis.micro_payments == 12
    assert episode.payment_ids == tuple(ids)
    assert (episode.micro_payments, episode.card_payments, episode.micro_share) == (12, 12, 1.0)
    assert (episode.distinct_cards, episode.flagged) == (4, True)


def test_a_window_without_payments_is_not_truncated(session_factory: sessionmaker[Session]) -> None:
    # Arrange: empty database

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert (analysis.micro_payments, analysis.episodes, analysis.truncated) == (0, (), False)


def test_exactly_the_limit_of_micro_payments_is_not_truncated(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _set_limit(monkeypatch, SMALL_LIMIT)
    ids = _micro_burst(session_factory, SMALL_LIMIT)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is False
    assert analysis.micro_payments == SMALL_LIMIT
    assert episode.payment_ids == tuple(ids)
    assert episode.micro_payments == SMALL_LIMIT


# --- above the limit --------------------------------------------------------------------------------------


def test_one_micro_payment_over_the_limit_drops_the_oldest_and_sets_truncated(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _set_limit(monkeypatch, SMALL_LIMIT)
    ids = _micro_burst(session_factory, SMALL_LIMIT + 1)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is True
    assert analysis.micro_payments == SMALL_LIMIT + 1
    assert episode.payment_ids == tuple(ids[1:])
    assert episode.first_payment_id == ids[1]
    assert (episode.micro_payments, episode.start) == (SMALL_LIMIT, BASE + timedelta(seconds=10))


def test_far_over_the_limit_keeps_the_newest_payments_and_counts_every_micro_payment_in_the_window(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: 30 payments 10 s apart; the newest 20 are kept
    _set_limit(monkeypatch, SMALL_LIMIT)
    ids = _micro_burst(session_factory, 30)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is True
    assert analysis.micro_payments == 30
    assert episode.payment_ids == tuple(ids[10:])
    assert (episode.start, episode.end) == (BASE + timedelta(seconds=100), BASE + timedelta(seconds=290))
    assert (episode.micro_payments, episode.card_payments, episode.micro_share) == (20, 20, 1.0)
    assert (episode.distinct_cards, episode.declined, episode.flagged) == (4, 20, True)


def test_payments_sharing_the_cut_instant_are_kept_by_the_larger_payment_id(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: 25 payments at one instant; "newest" by (occurred_at, payment_id) keeps the 20 largest ids
    _set_limit(monkeypatch, SMALL_LIMIT)
    with session_factory.begin() as session:
        ids = [_fact(session, 700 - index, BASE) for index in range(25)]

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is True
    assert analysis.micro_payments == 25
    assert episode.payment_ids == tuple(sorted(ids)[5:])
    assert (episode.micro_payments, episode.start, episode.end) == (20, BASE, BASE)


def test_an_older_episode_cut_below_ten_payments_is_left_out_and_the_newer_one_survives(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: 12 old payments and 15 new ones; the newest 20 are 15 new and 5 old
    _set_limit(monkeypatch, SMALL_LIMIT)
    _micro_burst(session_factory, 12, first_n=1, start=BASE)
    newer = _micro_burst(session_factory, 15, first_n=101, start=BASE + timedelta(hours=1), step_seconds=5)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert analysis.truncated is True
    assert analysis.micro_payments == 27
    assert [episode.payment_ids for episode in analysis.episodes] == [tuple(newer)]


def test_the_micro_payments_count_ignores_other_merchants_and_non_card_payments_when_truncated(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _set_limit(monkeypatch, SMALL_LIMIT)
    _micro_burst(session_factory, 22)
    with session_factory.begin() as session:
        for index in range(15):
            _fact(session, 5000 + index, BASE + timedelta(seconds=index), merchant_id=OTHER_MERCHANT)
            _fact(session, 6000 + index, BASE + timedelta(seconds=index), method="wallet")

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert analysis.micro_payments == 22
    assert analysis.truncated is True
    assert _only_episode(analysis).micro_payments == SMALL_LIMIT


# --- card_payments from one COUNT over the inclusive episode span -------------------------------------------


def test_card_payments_counts_every_card_payment_in_the_inclusive_span_of_the_kept_episode(
    session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: kept episode spans +100 s .. +290 s; larger payments sit on both ends, inside and outside
    _set_limit(monkeypatch, SMALL_LIMIT)
    _micro_burst(session_factory, 30)
    with session_factory.begin() as session:
        for n, offset in [(500, 100), (501, 290), (502, 155), (503, 95), (504, 291)]:
            _fact(session, n, BASE + timedelta(seconds=offset), amount_minor=101)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.start, episode.end) == (BASE + timedelta(seconds=100), BASE + timedelta(seconds=290))
    assert episode.micro_payments == 20
    assert episode.card_payments == 23
    assert episode.micro_share == 20 / 23


def test_card_payments_leaves_out_other_merchants_and_non_card_methods_in_the_span(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _micro_burst(session_factory, 12)
    with session_factory.begin() as session:
        for index in range(6):
            at = BASE + timedelta(seconds=index * 10 + 5)
            _fact(session, 500 + index, at, amount_minor=101, merchant_id=OTHER_MERCHANT)
            _fact(session, 600 + index, at, amount_minor=101, method="wallet")
        _fact(session, 700, BASE + timedelta(seconds=55), amount_minor=101)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.card_payments) == (12, 13)
    assert episode.micro_share == 12 / 13


def test_card_payments_counts_larger_payments_of_every_currency_in_the_span(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _micro_burst(session_factory, 12)
    with session_factory.begin() as session:
        _fact(session, 500, BASE + timedelta(seconds=5), amount_minor=2, currency="JPY")
        _fact(session, 501, BASE + timedelta(seconds=15), amount_minor=500, currency="EUR")

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.card_payments) == (12, 14)


# --- reads only micro payments ----------------------------------------------------------------------------


def test_analyze_fraud_does_not_load_non_micro_card_payments_from_postgres(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    # Arrange: 12 micro payments (+0 s .. +110 s) among 400 larger card payments one second apart,
    # 111 of which fall inside the episode span
    _micro_burst(session_factory, 12)
    _insert_series(session_factory, 400, first_n=10_000, start=BASE, amount_minor=5000)

    # Act
    with _recording(engine) as recorded:
        episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.card_payments) == (12, 123)
    assert max(recorded.row_counts) <= 12


def test_analyze_fraud_runs_no_per_episode_count_when_no_episode_reaches_ten_micro_payments(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    # Arrange: three episodes of 5 micro payments, an hour apart
    for index in range(3):
        _micro_burst(session_factory, 5, first_n=1 + index * 100, start=BASE + timedelta(hours=index))

    # Act
    with _recording(engine) as recorded:
        analysis = _analyze(session_factory)

    # Assert
    assert (analysis.micro_payments, analysis.episodes) == (15, ())
    assert len(recorded.row_counts) <= 2


@pytest.mark.parametrize(
    ("currency", "amount_minor"),
    [
        *[(currency, amount) for currency in sorted(CURRENCY_EXPONENT) for amount in (0, 1, 2, 99, 100, 101)],
        *[
            (currency, amount)
            for currency in ("USD", "EUR", "KWD", "XXX")
            for amount in (0, 99, 100, 101, 10_000)
        ],
    ],
)
def test_micro_condition_selects_exactly_what_is_micro_says(
    session_factory: sessionmaker[Session], currency: str, amount_minor: int
) -> None:
    # Arrange
    rules = import_module("ops_ai.fraud_watch.rules")
    with session_factory.begin() as session:
        payment_id = _fact(session, 1, BASE, amount_minor=amount_minor, currency=currency)

    # Act
    with session_factory() as session:
        selected = list(
            session.execute(select(PaymentFact.payment_id).where(rules.micro_condition())).scalars()
        )

    # Assert
    assert selected == ([payment_id] if is_micro(amount_minor, currency) else [])


# --- real-scale boundary (no patching) --------------------------------------------------------------------


def test_exactly_5000_micro_payments_are_analysed_in_full_without_patching_the_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_series(session_factory, SPEC_LIMIT)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is False
    assert analysis.micro_payments == SPEC_LIMIT
    assert episode.micro_payments == SPEC_LIMIT
    assert episode.card_payments == SPEC_LIMIT
    assert episode.payment_ids[0] == _series_id(1)
    assert episode.payment_ids[-1] == _series_id(SPEC_LIMIT)


def test_5001_micro_payments_drop_the_oldest_one_and_set_truncated_without_patching_the_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_series(session_factory, SPEC_LIMIT + 1)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    episode = _only_episode(analysis)
    assert analysis.truncated is True
    assert analysis.micro_payments == SPEC_LIMIT + 1
    assert episode.micro_payments == SPEC_LIMIT
    assert len(episode.payment_ids) == SPEC_LIMIT
    assert episode.payment_ids[0] == _series_id(2)
    assert episode.payment_ids[-1] == _series_id(SPEC_LIMIT + 1)
    assert episode.card_payments == SPEC_LIMIT


def test_the_micro_payments_of_a_larger_history_are_all_counted_while_older_ones_are_not_loaded(
    session_factory: sessionmaker[Session], engine: Engine
) -> None:
    # Arrange: 5200 payments one second apart
    _insert_series(session_factory, 5200)

    # Act
    with _recording(engine) as recorded:
        analysis = _analyze(session_factory)

    # Assert
    assert analysis.micro_payments == 5200
    assert analysis.truncated is True
    assert _only_episode(analysis).micro_payments == SPEC_LIMIT
    assert max(recorded.row_counts) <= SPEC_LIMIT
