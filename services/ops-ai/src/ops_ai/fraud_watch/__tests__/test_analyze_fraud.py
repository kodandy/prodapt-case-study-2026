"""Tests for analyze_fraud, written from docs/specs/sq3.md section 4 (episodes, features, scope)."""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.fraud_watch import CardTestingEpisode, FraudAnalysis, analyze_fraud
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

BASE = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
WINDOW_START = BASE - timedelta(hours=1)
WINDOW_END = BASE + timedelta(hours=2)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :method, :state, 'acq_a', NULL, :card_bin, "
    ":card_ref, 1, :occurred_at)"
)


def _card_ref(n: int) -> str:
    return f"{n:032x}"


def _fact(
    session: Session,
    n: int,
    at: datetime,
    *,
    amount_minor: int = 50,
    currency: str = "USD",
    state: str = "declined",
    card_ref: str | None = None,
    card_bin: str | None = "400000",
    merchant_id: uuid.UUID = MERCHANT,
    method: str = "card",
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
            "state": state,
            "card_bin": card_bin,
            "card_ref": card_ref,
            "occurred_at": at,
        },
    )
    return payment_id


def _burst(
    session_factory: sessionmaker[Session],
    *,
    count: int = 12,
    start: datetime = BASE,
    step_seconds: int = 10,
    first_n: int = 1,
    cards: int | None = 4,
    declined: int = 6,
    states: Sequence[str] | None = None,
    bins: Sequence[str | None] = ("400000",),
    currency: str = "USD",
    amount_minor: int = 50,
    merchant_id: uuid.UUID = MERCHANT,
    card_offset: int = 0,
) -> list[uuid.UUID]:
    """`count` micro card payments `step_seconds` apart; the first `declined` are declined."""
    ids: list[uuid.UUID] = []
    with session_factory.begin() as session:
        for index in range(count):
            state = states[index] if states is not None else ("declined" if index < declined else "succeeded")
            ids.append(
                _fact(
                    session,
                    first_n + index,
                    start + timedelta(seconds=index * step_seconds),
                    amount_minor=amount_minor,
                    currency=currency,
                    state=state,
                    card_ref=None if cards is None else _card_ref(card_offset + index % cards + 1),
                    card_bin=bins[index % len(bins)],
                    merchant_id=merchant_id,
                )
            )
    return ids


def _plain(
    session_factory: sessionmaker[Session],
    placements: Sequence[tuple[int, datetime]],
    *,
    amount_minor: int = 101,
    merchant_id: uuid.UUID = MERCHANT,
    method: str = "card",
    currency: str = "USD",
    card_bin: str | None = "999999",
) -> None:
    """Payments above the micro limit by default, one per (n, time)."""
    with session_factory.begin() as session:
        for n, at in placements:
            _fact(
                session,
                n,
                at,
                amount_minor=amount_minor,
                merchant_id=merchant_id,
                method=method,
                currency=currency,
                card_bin=card_bin,
                card_ref=_card_ref(900),
            )


def _analyze(
    session_factory: sessionmaker[Session],
    scope: Scope = SCOPE,
    start: datetime = WINDOW_START,
    end: datetime = WINDOW_END,
) -> FraudAnalysis:
    with session_factory() as session:
        return analyze_fraud(session, scope, start, end)


def _only_episode(analysis: FraudAnalysis) -> CardTestingEpisode:
    assert len(analysis.episodes) == 1
    return analysis.episodes[0]


# --- features of one episode ----------------------------------------------------------------------------


def test_analyze_fraud_reports_every_feature_of_a_card_testing_burst(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory, count=12, cards=4, declined=6, bins=("424242", "400000"))

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert analysis.micro_payments == 12
    assert analysis.episodes == (
        CardTestingEpisode(
            first_payment_id=ids[0],
            start=BASE,
            end=BASE + timedelta(seconds=110),
            payment_ids=tuple(ids),
            micro_payments=12,
            card_payments=12,
            micro_share=1.0,
            distinct_cards=4,
            declined=6,
            decline_ratio=0.5,
            card_bins=("400000", "424242"),
            currencies=("USD",),
            flagged=True,
            reasons=("micro_burst", "many_cards", "high_decline_ratio"),
        ),
    )


def test_analyze_fraud_returns_no_episode_when_the_merchant_has_no_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: empty database

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert analysis.micro_payments == 0
    assert analysis.episodes == ()


def test_analyze_fraud_orders_the_payment_ids_by_time_then_by_payment_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: ids fall as time rises, and the last two payments share one instant
    with session_factory.begin() as session:
        ids = [
            _fact(
                session,
                5000 - index,
                BASE + timedelta(seconds=min(index, 8) * 10),
                card_ref=_card_ref(index % 4),
            )
            for index in range(10)
        ]

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.payment_ids == (*ids[:8], ids[9], ids[8])
    assert episode.first_payment_id == ids[0]
    assert (episode.start, episode.end) == (BASE, BASE + timedelta(seconds=80))


def test_analyze_fraud_breaks_a_tie_on_one_instant_by_the_smaller_payment_id_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        ids = [_fact(session, 700 - index, BASE, card_ref=_card_ref(index % 4)) for index in range(10)]

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.payment_ids == tuple(sorted(ids))
    assert episode.first_payment_id == min(ids)
    assert (episode.start, episode.end) == (BASE, BASE)


# --- episodes by the 120 s gap rule ---------------------------------------------------------------------


def test_analyze_fraud_keeps_a_gap_of_exactly_120_seconds_inside_one_episode(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _burst(session_factory, count=10, first_n=1, start=BASE)
    second = _burst(session_factory, count=10, first_n=101, start=BASE + timedelta(seconds=90 + 120))

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.payment_ids == tuple(first + second)
    assert episode.micro_payments == 20


def test_analyze_fraud_splits_a_gap_of_121_seconds_into_two_episodes_newest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _burst(session_factory, count=10, first_n=1, start=BASE)
    second = _burst(session_factory, count=10, first_n=101, start=BASE + timedelta(seconds=90 + 121))

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert [episode.payment_ids for episode in analysis.episodes] == [tuple(second), tuple(first)]
    assert [episode.start for episode in analysis.episodes] == [BASE + timedelta(seconds=211), BASE]
    assert [episode.card_payments for episode in analysis.episodes] == [10, 10]
    assert [episode.flagged for episode in analysis.episodes] == [True, True]


def test_analyze_fraud_does_not_bridge_a_long_gap_with_a_payment_above_the_micro_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _burst(session_factory, count=10, first_n=1, start=BASE)
    second = _burst(session_factory, count=10, first_n=101, start=BASE + timedelta(seconds=290))
    _plain(session_factory, [(500, BASE + timedelta(seconds=190))])

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert [episode.payment_ids for episode in analysis.episodes] == [tuple(second), tuple(first)]


def test_analyze_fraud_leaves_out_an_episode_of_nine_micro_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=9)

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert analysis.episodes == ()


def test_analyze_fraud_keeps_an_episode_of_exactly_ten_micro_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory, count=10, cards=4, declined=0)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.payment_ids, episode.micro_payments, episode.flagged) == (tuple(ids), 10, True)
    assert episode.reasons == ("micro_burst", "many_cards")


def test_analyze_fraud_leaves_out_two_episodes_of_six_that_a_long_gap_separates(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=6, first_n=1, start=BASE)
    _burst(session_factory, count=6, first_n=101, start=BASE + timedelta(seconds=50 + 180))

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert analysis.episodes == ()


# --- micro amounts by currency --------------------------------------------------------------------------


def test_analyze_fraud_counts_micro_payments_per_currency_and_lists_the_sorted_distinct_currencies(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: six USD and six JPY micro payments, one JPY and one USD payment above the limit
    usd = _burst(
        session_factory, count=6, first_n=1, start=BASE, step_seconds=20, currency="USD", amount_minor=100
    )
    jpy = _burst(
        session_factory,
        count=6,
        first_n=101,
        start=BASE + timedelta(seconds=10),
        step_seconds=20,
        currency="JPY",
        amount_minor=1,
        card_offset=10,
    )
    _plain(session_factory, [(500, BASE + timedelta(seconds=15))], amount_minor=2, currency="JPY")
    _plain(session_factory, [(501, BASE + timedelta(seconds=25))], amount_minor=101, currency="USD")

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert set(episode.payment_ids) == set(usd + jpy)
    assert episode.micro_payments == 12
    assert episode.card_payments == 14
    assert episode.micro_share == 12 / 14
    assert episode.currencies == ("JPY", "USD")


def test_analyze_fraud_lists_sorted_distinct_non_null_bins_of_the_micro_payments_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=12, bins=("424242", None, "400000", "424242"))
    _plain(session_factory, [(500, BASE + timedelta(seconds=5))], card_bin="555555")

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.card_bins == ("400000", "424242")


def test_analyze_fraud_has_no_bins_when_every_micro_payment_lacks_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=12, bins=(None,))

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.card_bins == ()


# --- card payments and micro share ----------------------------------------------------------------------


def test_analyze_fraud_counts_all_card_payments_inside_the_span_so_half_micro_is_not_flagged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 12 micro payments, 12 larger ones, the last of them exactly at the episode end
    ids = _burst(session_factory, count=12, cards=4, declined=6)
    placements = [(500 + index, BASE + timedelta(seconds=index * 10 + 5)) for index in range(11)]
    placements.append((600, BASE + timedelta(seconds=110)))
    _plain(session_factory, placements)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.payment_ids == tuple(ids)
    assert (episode.micro_payments, episode.card_payments, episode.micro_share) == (12, 24, 0.5)
    assert (episode.flagged, episode.reasons) == (False, ())


def test_analyze_fraud_counts_a_larger_payment_exactly_at_the_episode_start(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=12)
    _plain(session_factory, [(500, BASE)])

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.card_payments, episode.micro_share) == (13, 12 / 13)


def test_analyze_fraud_ignores_larger_payments_just_outside_the_episode_span(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=12)
    _plain(
        session_factory,
        [(500, BASE - timedelta(seconds=1)), (501, BASE + timedelta(seconds=111))],
    )

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.card_payments, episode.micro_share, episode.flagged) == (12, 1.0, True)


def test_analyze_fraud_ignores_payments_that_are_not_card_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory, count=12)
    _plain(
        session_factory,
        [(500 + index, BASE + timedelta(seconds=index * 10)) for index in range(5)],
        method="wallet",
    )
    _plain(
        session_factory,
        [(600 + index, BASE + timedelta(seconds=index * 10 + 1)) for index in range(5)],
        method="wallet",
        amount_minor=50,
    )

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.payment_ids == tuple(ids)
    assert (episode.micro_payments, episode.card_payments) == (12, 12)


def test_analyze_fraud_flags_a_micro_share_of_exactly_80_percent(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 16 micro payments 5 s apart, 4 larger ones inside the span
    _burst(session_factory, count=16, step_seconds=5, cards=4, declined=0)
    _plain(session_factory, [(500 + index, BASE + timedelta(seconds=index * 15 + 2)) for index in range(4)])

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.card_payments, episode.micro_share) == (16, 20, 0.8)
    assert (episode.flagged, episode.reasons) == (True, ("micro_burst", "many_cards"))


def test_analyze_fraud_does_not_flag_a_micro_share_just_under_80_percent(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 15 micro payments, 4 larger ones inside the span
    _burst(session_factory, count=15, step_seconds=5, cards=4, declined=0)
    _plain(session_factory, [(500 + index, BASE + timedelta(seconds=index * 15 + 2)) for index in range(4)])

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.card_payments, episode.micro_share) == (15, 19, 15 / 19)
    assert (episode.flagged, episode.reasons) == (False, ())


# --- distinct cards -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cards", "flagged", "reasons"),
    [
        (1, False, ()),
        (2, False, ()),
        (3, True, ("micro_burst", "many_cards")),
        (4, True, ("micro_burst", "many_cards")),
    ],
    ids=["one_card", "two_cards", "three_cards", "four_cards"],
)
def test_analyze_fraud_flags_from_three_distinct_cards(
    session_factory: sessionmaker[Session], cards: int, flagged: bool, reasons: tuple[str, ...]
) -> None:
    # Arrange
    _burst(session_factory, count=12, cards=cards, declined=0)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.distinct_cards == cards
    assert (episode.flagged, episode.reasons) == (flagged, reasons)


def test_analyze_fraud_reports_unknown_card_identity_as_none_and_still_flags_the_burst(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=12, cards=None, declined=0)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.distinct_cards is None
    assert (episode.flagged, episode.reasons) == (True, ("micro_burst", "card_identity_unknown"))


def test_analyze_fraud_reports_none_when_even_one_micro_payment_has_no_card_ref(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: eleven payments on four cards, one without a card_ref
    _burst(session_factory, count=11, first_n=1, start=BASE, cards=4, declined=0)
    with session_factory.begin() as session:
        _fact(session, 99, BASE + timedelta(seconds=110), card_ref=None)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.distinct_cards) == (12, None)
    assert (episode.flagged, episode.reasons) == (True, ("micro_burst", "card_identity_unknown"))


def test_analyze_fraud_counts_each_card_ref_once_however_often_it_repeats(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 24 payments on 3 card_refs
    _burst(session_factory, count=24, step_seconds=5, cards=3, declined=0)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.micro_payments, episode.distinct_cards) == (24, 3)


# --- declines -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("declined", "expected_ratio", "reasons"),
    [
        (0, 0.0, ("micro_burst", "many_cards")),
        (5, 5 / 12, ("micro_burst", "many_cards")),
        (6, 0.5, ("micro_burst", "many_cards", "high_decline_ratio")),
        (12, 1.0, ("micro_burst", "many_cards", "high_decline_ratio")),
    ],
    ids=["none_declined", "five_of_twelve", "exactly_half", "all_declined"],
)
def test_analyze_fraud_adds_high_decline_ratio_from_half_declined(
    session_factory: sessionmaker[Session], declined: int, expected_ratio: float, reasons: tuple[str, ...]
) -> None:
    # Arrange
    _burst(session_factory, count=12, cards=4, declined=declined)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert episode.declined == declined
    assert episode.decline_ratio == expected_ratio
    assert episode.reasons == reasons


def test_analyze_fraud_counts_declined_and_failed_but_no_other_state_as_declined(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    states = ["declined"] * 2 + ["failed"] * 2 + ["succeeded"] * 3 + ["unknown"] * 3 + ["created"] * 2
    _burst(session_factory, count=12, states=states)

    # Act
    episode = _only_episode(_analyze(session_factory))

    # Assert
    assert (episode.declined, episode.decline_ratio) == (4, 4 / 12)
    assert "high_decline_ratio" not in episode.reasons


# --- window, scope and purity ---------------------------------------------------------------------------


def test_analyze_fraud_includes_a_payment_at_start_and_excludes_one_at_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: payments at +0 s ... +110 s; the window is [+10 s, +110 s)
    ids = _burst(session_factory, count=12)

    # Act
    episode = _only_episode(
        _analyze(session_factory, start=BASE + timedelta(seconds=10), end=BASE + timedelta(seconds=110))
    )

    # Assert
    assert episode.payment_ids == tuple(ids[1:11])
    assert (episode.start, episode.end) == (BASE + timedelta(seconds=10), BASE + timedelta(seconds=100))


def test_analyze_fraud_ignores_payments_outside_the_window(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _burst(session_factory, count=12)

    # Act
    analysis = _analyze(session_factory, start=BASE + timedelta(hours=1), end=BASE + timedelta(hours=2))

    # Assert
    assert analysis.episodes == ()


def test_analyze_fraud_never_shows_another_merchants_payments(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _burst(session_factory, count=12, first_n=1000, merchant_id=OTHER_MERCHANT)

    # Act
    analysis = _analyze(session_factory, scope=SCOPE)

    # Assert
    assert analysis.micro_payments == 0
    assert analysis.episodes == ()


def test_analyze_fraud_keeps_two_merchants_bursts_in_the_same_span_apart(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    own = _burst(session_factory, count=12, first_n=1, cards=4, card_offset=0)
    others = _burst(
        session_factory,
        count=12,
        first_n=1000,
        cards=3,
        card_offset=50,
        merchant_id=OTHER_MERCHANT,
        declined=12,
    )
    _plain(
        session_factory,
        [(2000 + index, BASE + timedelta(seconds=index * 10 + 3)) for index in range(11)],
        merchant_id=OTHER_MERCHANT,
    )

    # Act
    own_episode = _only_episode(_analyze(session_factory, scope=SCOPE))
    other_episode = _only_episode(_analyze(session_factory, scope=OTHER_SCOPE))

    # Assert
    assert own_episode.payment_ids == tuple(own)
    assert (own_episode.card_payments, own_episode.distinct_cards, own_episode.declined) == (12, 4, 6)
    assert other_episode.payment_ids == tuple(others)
    assert (other_episode.card_payments, other_episode.distinct_cards, other_episode.declined) == (23, 3, 12)


def test_analyze_fraud_writes_no_alert_no_ticket_and_no_payment_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, count=12)

    # Act
    _analyze(session_factory)

    # Assert
    with session_factory() as session:
        counts = session.execute(
            text(
                "SELECT (SELECT count(*) FROM ops_ai.fraud_alerts), (SELECT count(*) FROM ops_ai.tickets), "
                "(SELECT count(*) FROM ops_ai.payment_facts)"
            )
        ).one()
    assert tuple(counts) == (0, 0, 12)


def test_analyze_fraud_orders_episodes_newest_start_first(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    oldest = _burst(session_factory, count=10, first_n=1, start=BASE)
    middle = _burst(session_factory, count=10, first_n=101, start=BASE + timedelta(minutes=10))
    newest = _burst(session_factory, count=10, first_n=201, start=BASE + timedelta(minutes=20))

    # Act
    analysis = _analyze(session_factory)

    # Assert
    assert [episode.first_payment_id for episode in analysis.episodes] == [newest[0], middle[0], oldest[0]]


def test_the_episode_and_the_analysis_are_immutable(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _burst(session_factory, count=12)
    analysis = _analyze(session_factory)
    episode = _only_episode(analysis)

    # Act / Assert
    with pytest.raises(AttributeError):
        episode.flagged = False  # type: ignore[misc]
    with pytest.raises(AttributeError):
        analysis.episodes = ()  # type: ignore[misc]
