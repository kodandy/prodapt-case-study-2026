"""Tests for ops_ai/investigation/tools.py, written from docs/specs/sq1.md section 6 (tools)."""

import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.tools import (
    BinShare,
    CodeCount,
    FailureAggregate,
    IncidentHit,
    aggregate_failures,
    compare_baseline,
    search_incidents,
)
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

START = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
END = START + timedelta(hours=6)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
STARTED_AT = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
RESOLVED_AT = datetime(2026, 9, 14, 16, 40, tzinfo=UTC)
QUERY = "BIN 400000 issuer"


def _fact(
    session: Session,
    occurred_at: datetime,
    *,
    state: str = "declined",
    card_bin: str | None = "400000",
    decline_code: str | None = None,
    merchant_id: uuid.UUID = MERCHANT,
    method: str = "card",
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    if decline_code is None and state in ("declined", "failed"):
        decline_code = "card_declined"
    session.add(
        PaymentFact(
            payment_id=payment_id,
            merchant_id=merchant_id,
            amount_minor=1500,
            currency="USD",
            method=method,
            state=state,
            provider="acq_a",
            decline_code=decline_code,
            card_bin=card_bin,
            entity_version=1,
            occurred_at=occurred_at,
        )
    )
    return payment_id


def _incident(
    session: Session,
    incident_id: str,
    merchant_id: uuid.UUID | None,
    *,
    title: str = "Issuer outage on BIN 400000",
    root_cause: str = "Issuer returned timeouts on BIN 400000",
    fix_applied: str = "Rerouted BIN 400000, the issuer was down",
    resolved: bool = True,
    recurrence: bool = False,
) -> None:
    session.execute(
        text(
            "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
            "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
            "(:incident_id, :merchant_id, :title, :root_cause, :fix_applied, 'notes', :started_at, "
            ":resolved_at, :recurrence)"
        ),
        {
            "incident_id": incident_id,
            "merchant_id": merchant_id,
            "title": title,
            "root_cause": root_cause,
            "fix_applied": fix_applied,
            "started_at": STARTED_AT,
            "resolved_at": RESOLVED_AT if resolved else None,
            "recurrence": recurrence,
        },
    )


# --- aggregate_failures ----------------------------------------------------------------------------------


def test_aggregate_failures_returns_zeroes_and_empty_tuples_for_a_window_without_records(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        result = aggregate_failures(session, SCOPE, START, END)

    # Assert
    assert result == FailureAggregate(
        start=START, end=END, total=0, failed=0, rate=0.0, by_bin=(), by_code=()
    )


def test_aggregate_failures_counts_every_state_in_total_and_declined_or_failed_as_failed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for minute, state in enumerate(
            ["succeeded"] * 4 + ["unknown", "created", "declined", "declined", "failed"]
        ):
            _fact(session, START + timedelta(minutes=minute), state=state)

    # Act
    with session_factory() as session:
        result = aggregate_failures(session, SCOPE, START, END)

    # Assert
    assert (result.total, result.failed) == (9, 3)
    assert result.rate == 3 / 9


def test_aggregate_failures_includes_the_start_instant_and_excludes_the_end_instant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START - timedelta(seconds=1))
        _fact(session, START)
        _fact(session, END - timedelta(seconds=1))
        _fact(session, END)

    # Act
    with session_factory() as session:
        result = aggregate_failures(session, SCOPE, START, END)

    # Assert
    assert (result.total, result.failed) == (2, 2)


def test_aggregate_failures_echoes_the_requested_start_and_end(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    start = START + timedelta(minutes=30)
    end = END - timedelta(minutes=30)

    # Act
    with session_factory() as session:
        result = aggregate_failures(session, SCOPE, start, end)

    # Assert
    assert (result.start, result.end) == (start, end)


def test_aggregate_failures_ignores_payments_that_are_not_card(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=1), method="wallet")
        _fact(session, START + timedelta(minutes=2), method="card")

    # Act
    with session_factory() as session:
        result = aggregate_failures(session, SCOPE, START, END)

    # Assert
    assert (result.total, result.failed) == (1, 1)


def test_aggregate_failures_never_counts_another_merchants_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START + timedelta(minutes=1), state="succeeded")
        for minute in range(2, 8):
            _fact(session, START + timedelta(minutes=minute), merchant_id=OTHER_MERCHANT)

    # Act
    with session_factory() as session:
        own = aggregate_failures(session, SCOPE, START, END)
        other = aggregate_failures(session, Scope(merchant_id=OTHER_MERCHANT), START, END)

    # Assert
    assert (own.total, own.failed, own.rate, own.by_bin, own.by_code) == (1, 0, 0.0, (), ())
    assert (other.total, other.failed) == (6, 6)


def test_aggregate_failures_by_bin_lists_failed_bins_by_failures_then_bin_and_leaves_out_null_and_clean_bins(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    plan: list[tuple[str | None, str, str | None]] = [
        ("424242", "declined", "card_declined"),
        ("424242", "declined", "card_declined"),
        ("424242", "succeeded", None),
        ("400000", "declined", "issuer_unavailable"),
        ("400000", "declined", "issuer_unavailable"),
        ("400000", "failed", "issuer_unavailable"),
        ("400000", "succeeded", None),
        ("400000", "succeeded", None),
        ("411111", "declined", "insufficient_funds"),
        ("411111", "failed", "insufficient_funds"),
        ("411111", "succeeded", None),
        ("411111", "succeeded", None),
        ("555555", "succeeded", None),
        ("555555", "succeeded", None),
        ("555555", "succeeded", None),
        (None, "declined", "card_declined"),
        (None, "declined", "do_not_honor"),
    ]
    with session_factory.begin() as session:
        for minute, (card_bin, state, code) in enumerate(plan):
            _fact(
                session, START + timedelta(minutes=minute), state=state, card_bin=card_bin, decline_code=code
            )

    # Act
    with session_factory() as session:
        result = aggregate_failures(session, SCOPE, START, END)

    # Assert
    assert (result.total, result.failed) == (17, 9)
    assert result.by_bin == (
        BinShare(card_bin="400000", failed=3, total=5),
        BinShare(card_bin="411111", failed=2, total=4),
        BinShare(card_bin="424242", failed=2, total=3),
    )


def test_aggregate_failures_by_code_counts_failed_payments_by_count_then_code(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    plan: list[tuple[str, str | None]] = [
        ("declined", "card_declined"),
        ("declined", "card_declined"),
        ("declined", "card_declined"),
        ("declined", "issuer_unavailable"),
        ("failed", "issuer_unavailable"),
        ("declined", "issuer_unavailable"),
        ("declined", "insufficient_funds"),
        ("failed", "insufficient_funds"),
        ("declined", "do_not_honor"),
        ("succeeded", None),
        ("succeeded", None),
    ]
    with session_factory.begin() as session:
        for minute, (state, code) in enumerate(plan):
            _fact(session, START + timedelta(minutes=minute), state=state, decline_code=code)

    # Act
    with session_factory() as session:
        result = aggregate_failures(session, SCOPE, START, END)

    # Assert
    assert result.by_code == (
        CodeCount(decline_code="card_declined", count=3),
        CodeCount(decline_code="issuer_unavailable", count=3),
        CodeCount(decline_code="insufficient_funds", count=2),
        CodeCount(decline_code="do_not_honor", count=1),
    )


# --- compare_baseline ------------------------------------------------------------------------------------


def test_compare_baseline_aggregates_the_seven_days_before_the_window_start_by_default(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START - timedelta(days=7, seconds=1), state="succeeded")
        _fact(session, START - timedelta(days=7), state="declined")
        _fact(session, START - timedelta(days=3), state="succeeded")
        _fact(session, START - timedelta(seconds=1), state="succeeded")
        _fact(session, START, state="declined")

    # Act
    with session_factory() as session:
        result = compare_baseline(session, SCOPE, START)

    # Assert
    assert result.start == START - timedelta(days=7)
    assert result.end == START
    assert (result.total, result.failed, result.rate) == (3, 1, 1 / 3)


def test_compare_baseline_uses_the_given_number_of_days(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START - timedelta(days=2), state="declined")
        _fact(session, START - timedelta(hours=23), state="succeeded")
        _fact(session, START - timedelta(hours=1), state="succeeded")

    # Act
    with session_factory() as session:
        one_day = compare_baseline(session, SCOPE, START, days=1)
        three_days = compare_baseline(session, SCOPE, START, days=3)

    # Assert
    assert (one_day.start, one_day.end, one_day.total, one_day.failed) == (
        START - timedelta(days=1),
        START,
        2,
        0,
    )
    assert (three_days.start, three_days.total, three_days.failed) == (START - timedelta(days=3), 3, 1)


def test_compare_baseline_equals_aggregate_failures_over_the_preceding_days(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for hour in range(0, 160, 7):
            _fact(session, START - timedelta(hours=hour + 1), state="succeeded" if hour % 3 else "declined")

    # Act
    with session_factory() as session:
        baseline = compare_baseline(session, SCOPE, START, days=7)
        direct = aggregate_failures(session, SCOPE, START - timedelta(days=7), START)

    # Assert
    assert baseline == direct


def test_compare_baseline_never_counts_another_merchants_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _fact(session, START - timedelta(days=1), state="succeeded")
        _fact(session, START - timedelta(days=1), merchant_id=OTHER_MERCHANT)
        _fact(session, START - timedelta(days=2), merchant_id=OTHER_MERCHANT)

    # Act
    with session_factory() as session:
        result = compare_baseline(session, SCOPE, START)

    # Assert
    assert (result.total, result.failed, result.rate) == (1, 0, 0.0)


# --- search_incidents ------------------------------------------------------------------------------------


def test_search_incidents_returns_the_hit_fields(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_a", MERCHANT, recurrence=True)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, SCOPE, QUERY)

    # Assert
    assert hits == [
        IncidentHit(
            incident_id="inc_a",
            title="Issuer outage on BIN 400000",
            root_cause="Issuer returned timeouts on BIN 400000",
            fix_applied="Rerouted BIN 400000, the issuer was down",
            resolved_at=RESOLVED_AT,
            recurrence_within_7d=True,
        )
    ]


def test_search_incidents_returns_the_merchants_and_platform_wide_incidents_but_never_another_merchants(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_own", MERCHANT)
        _incident(session, "inc_platform", None)
        _incident(session, "inc_other", OTHER_MERCHANT)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, SCOPE, QUERY)
        other_hits = search_incidents(session, Scope(merchant_id=OTHER_MERCHANT), QUERY)

    # Assert
    assert sorted(hit.incident_id for hit in hits) == ["inc_own", "inc_platform"]
    assert sorted(hit.incident_id for hit in other_hits) == ["inc_other", "inc_platform"]


def test_search_incidents_for_a_merchant_without_incidents_still_sees_the_platform_wide_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_platform", None)
        _incident(session, "inc_other", OTHER_MERCHANT)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, Scope(merchant_id=uuid.uuid4()), QUERY)

    # Assert
    assert [hit.incident_id for hit in hits] == ["inc_platform"]


def test_search_incidents_lists_resolved_incidents_before_unresolved_ones_whatever_the_rank(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    strong_text = "issuer issuer issuer issuer BIN 400000 issuer BIN 400000"
    with session_factory.begin() as session:
        _incident(session, "inc_unresolved_strong", MERCHANT, title=strong_text, resolved=False)
        _incident(session, "inc_resolved_weak", MERCHANT, resolved=True)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, SCOPE, QUERY)

    # Assert
    assert [hit.incident_id for hit in hits] == ["inc_resolved_weak", "inc_unresolved_strong"]
    assert [hit.resolved_at for hit in hits] == [RESOLVED_AT, None]


def test_search_incidents_ranks_resolved_incidents_by_relevance(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    strong_text = "issuer issuer issuer issuer BIN 400000 issuer BIN 400000"
    with session_factory.begin() as session:
        _incident(session, "inc_weak", MERCHANT)
        _incident(session, "inc_strong", MERCHANT, title=strong_text)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, SCOPE, QUERY)

    # Assert
    assert [hit.incident_id for hit in hits] == ["inc_strong", "inc_weak"]


def test_search_incidents_returns_an_empty_list_when_nothing_matches(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _incident(session, "inc_a", MERCHANT)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, SCOPE, "chargeback settlement")

    # Assert
    assert hits == []


def test_search_incidents_default_limit_is_five_and_the_limit_argument_is_honoured(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for n in range(7):
            _incident(session, f"inc_{n}", MERCHANT)

    # Act
    with session_factory() as session:
        default_hits = search_incidents(session, SCOPE, QUERY)
        two_hits = search_incidents(session, SCOPE, QUERY, limit=2)

    # Assert
    assert len(default_hits) == 5
    assert len(two_hits) == 2
