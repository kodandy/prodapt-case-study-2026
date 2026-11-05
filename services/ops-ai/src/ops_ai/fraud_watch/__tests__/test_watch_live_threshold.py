"""Live threshold and applied-version contracts from r2-agents.md section 6.2."""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

import pytest
from contracts.events import PaymentEventV1
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.clock import Clock
from ops_ai.feedback.thresholds import RuleVersion, append_rule_version, revert_rule, thresholds_for_steps
from ops_ai.fraud_watch.analysis import FraudAnalysis
from ops_ai.fraud_watch.service import investigate_fraud
from ops_ai.fraud_watch.watch import applied_rule_version, watch_payment
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import Scope
from ops_ai.seed import merchant_uuid, seed_dataset
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import generate

BASE = datetime(2026, 11, 3, 10, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
Origin = Literal["live", "backfill"]


def _fixed_clock(at: datetime) -> Clock:
    return lambda: at


def _event(
    n: int,
    at: datetime,
    *,
    merchant: uuid.UUID = MERCHANT,
    card: int | None = 1,
    amount: int = 50,
    origin: Origin = "live",
    declined: bool = True,
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=100_000 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin=origin,
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant,
        amount_minor=amount,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="card_declined" if declined else None,
        card_bin="400000",
        card_last4="0002",
        card_ref=None if card is None else f"{card:032x}",
    )


def _burst(
    *,
    count: int = 12,
    cards: int | None = 4,
    first: int = 1,
    start: datetime = BASE,
    merchant: uuid.UUID = MERCHANT,
    spacing: int = 10,
) -> list[PaymentEventV1]:
    return [
        _event(
            first + index,
            start + timedelta(seconds=index * spacing),
            merchant=merchant,
            card=None if cards is None else index % cards + 1,
            declined=index < 6,
        )
        for index in range(count)
    ]


def _project(session: Session, clock: Clock, events: list[PaymentEventV1]) -> None:
    for event in events:
        apply_payment_event(session, clock, event.model_copy(update={"origin": "backfill"}))


def _rows(session: Session, table: str) -> list[dict[str, object]]:
    queries = {
        "fraud_alerts": "SELECT * FROM ops_ai.fraud_alerts",
        "tickets": "SELECT * FROM ops_ai.tickets",
        "ops_alerts": "SELECT * FROM ops_ai.ops_alerts",
        "ops_alert_deliveries": "SELECT * FROM ops_ai.ops_alert_deliveries",
        "fraud_rule_versions": "SELECT * FROM ops_ai.fraud_rule_versions",
        "fraud_rule_version_actors": "SELECT * FROM ops_ai.fraud_rule_version_actors",
        "fraud_alert_rule_versions": "SELECT * FROM ops_ai.fraud_alert_rule_versions",
    }
    return [dict(row) for row in session.execute(text(queries[table])).mappings()]


def _counts(session: Session) -> tuple[int, int, int, int]:
    return (
        len(_rows(session, "fraud_alerts")),
        len(_rows(session, "tickets")),
        len(_rows(session, "ops_alerts")),
        len(_rows(session, "fraud_alert_rule_versions")),
    )


def _append(session: Session, clock: Clock, steps: int, merchant: uuid.UUID = MERCHANT) -> int:
    return append_rule_version(
        session, Scope(merchant), clock, steps, thresholds_for_steps(steps), "test:threshold", None
    ).version


def test_baseline_burst_keeps_identical_alert_ticket_ops_alert_and_single_log_bytes(
    session_factory: sessionmaker[Session], clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    events = _burst()
    caplog.set_level(logging.INFO, logger="ops_ai.fraud_watch.watch")
    with session_factory.begin() as session:
        _project(session, clock, events)

        # Act
        result = watch_payment(session, clock, events[-1])
        repeated = watch_payment(session, clock, events[-1])

        # Assert
        (alert,) = _rows(session, "fraud_alerts")
        (ticket,) = _rows(session, "tickets")
        (ops_alert,) = _rows(session, "ops_alerts")
        (delivery,) = _rows(session, "ops_alert_deliveries")
        assert result == alert["alert_id"]
        assert repeated == result
        source_key = f"card_testing:{MERCHANT}:{events[0].payment_id}"
        reason = "12 micro card payments on 4 distinct cards over 1 min 50 s"
        expected_alert = {
            "alert_id": result,
            "window_key": source_key,
            "merchant_id": MERCHANT,
            "kind": "card_testing",
            "first_payment_id": events[0].payment_id,
            "started_at": BASE,
            "last_seen_at": BASE + timedelta(seconds=110),
            "payment_count": 12,
            "features": {
                "micro_payments": 12,
                "card_payments": 12,
                "micro_share": 1.0,
                "distinct_cards": 4,
                "declined": 6,
                "decline_ratio": 0.5,
                "card_bins": ["400000"],
                "currencies": ["USD"],
                "reasons": ["micro_burst", "many_cards", "high_decline_ratio"],
            },
            "created_at": clock(),
        }
        expected_ticket = {
            "ticket_id": ticket["ticket_id"],
            "source_key": source_key,
            "source": "card_testing",
            "queue": "fraud",
            "priority": "high",
            "status": "open",
            "merchant_id": MERCHANT,
            "title": "Card testing suspected",
            "reason": reason,
            "assignee": None,
            "alert_id": result,
            "created_at": clock(),
            "updated_at": None,
        }
        expected_ops = {
            "alert_id": ops_alert["alert_id"],
            "source_key": source_key,
            "kind": "card_testing",
            "severity": "high",
            "merchant_id": MERCHANT,
            "fraud_alert_id": result,
            "title": "Card testing suspected",
            "body": reason,
            "created_at": clock(),
            "acknowledged_at": None,
            "acknowledged_by": None,
        }
        assert (
            json.dumps([alert, ticket, ops_alert], sort_keys=True, default=str).encode()
            == json.dumps(
                [expected_alert, expected_ticket, expected_ops], sort_keys=True, default=str
            ).encode()
        )
        assert delivery == {
            "delivery_id": delivery["delivery_id"],
            "alert_id": ops_alert["alert_id"],
            "channel": "redis_stream",
            "status": "pending",
            "attempts": 0,
            "stream_entry_id": None,
            "last_error": None,
            "created_at": clock(),
            "delivered_at": None,
        }
        assert _rows(session, "fraud_rule_versions") == []
        assert _rows(session, "fraud_alert_rule_versions") == [
            {"alert_id": result, "merchant_id": MERCHANT, "rule_version": 0, "applied_at": clock()}
        ]
        lines = [r.getMessage().encode() for r in caplog.records if "fraud alert raised" in r.getMessage()]
        assert lines == [
            f"fraud alert raised alert_id={result} merchant_id={MERCHANT} payment_count=12".encode()
        ]


@pytest.mark.parametrize(("steps", "expected_version"), [(0, 0), (1, 1), (2, None)])
def test_sq3_twelve_micro_four_card_example_uses_active_thresholds(
    session_factory: sessionmaker[Session], clock: Clock, steps: int, expected_version: int | None
) -> None:
    # Arrange
    events = _burst()
    with session_factory.begin() as session:
        if steps:
            _append(session, clock, steps)
        _project(session, clock, events)

        # Act
        result = watch_payment(session, clock, events[-1])

        # Assert
        if expected_version is None:
            assert result is None
            assert _counts(session) == (0, 0, 0, 0)
            assert _rows(session, "ops_alert_deliveries") == []
        else:
            (alert,) = _rows(session, "fraud_alerts")
            assert result == alert["alert_id"]
            assert _counts(session) == (1, 1, 1, 1)
            assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, result)) == expected_version
            assert _rows(session, "fraud_alert_rule_versions") == [
                {
                    "alert_id": result,
                    "merchant_id": MERCHANT,
                    "rule_version": expected_version,
                    "applied_at": clock(),
                }
            ]


def test_revert_to_baseline_reenables_the_next_burst_and_records_revert_version(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    first = _burst()
    second = _burst(first=100, start=BASE + timedelta(minutes=10))
    with session_factory.begin() as session:
        _append(session, clock, 2)
        _project(session, clock, first)
        suppressed = watch_payment(session, clock, first[-1])
        reverted = revert_rule(session, Scope(MERCHANT), clock, 0)
        _project(session, clock, second)

        # Act
        result = watch_payment(session, clock, second[-1])

        # Assert
        assert suppressed is None
        assert reverted == RuleVersion(2, 0, 10, 0.8, 3, "revert:v0", None, clock())
        (alert,) = _rows(session, "fraud_alerts")
        assert (result, alert["first_payment_id"], alert["payment_count"]) == (
            alert["alert_id"],
            second[0].payment_id,
            12,
        )
        assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, result)) == 2
        assert _counts(session) == (1, 1, 1, 1)


def test_active_rule_is_read_from_the_same_uncommitted_transaction(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    with session_factory() as session:
        _project(session, clock, events)
        _append(session, clock, 2)

        # Act
        result = watch_payment(session, clock, events[-1])
        session.rollback()

    # Assert
    assert result is None
    with session_factory() as session:
        assert _counts(session) == (0, 0, 0, 0)
        assert _rows(session, "fraud_rule_versions") == []


def test_latest_version_number_controls_alerting_even_when_an_older_rule_is_stricter(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    with session_factory.begin() as session:
        _append(session, clock, 2)
        _append(session, clock, 1)
        _project(session, clock, events)

        # Act
        result = watch_payment(session, clock, events[-1])

        # Assert
        (alert,) = _rows(session, "fraud_alerts")
        assert result == alert["alert_id"]
        assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, result)) == 2
        assert _counts(session) == (1, 1, 1, 1)


@pytest.mark.parametrize(("count", "cards", "large_count"), [(11, 4, 0), (12, 3, 0), (12, 4, 3)])
def test_active_rule_requires_each_count_card_and_micro_share_threshold(
    session_factory: sessionmaker[Session], clock: Clock, count: int, cards: int, large_count: int
) -> None:
    # Arrange: fail one threshold at a time; three large payments yield baseline share 0.8 < 0.83.
    events = _burst(count=count, cards=cards)
    large = [_event(500 + i, BASE + timedelta(seconds=i * 10 + 5), amount=5000) for i in range(large_count)]
    with session_factory.begin() as session:
        _append(session, clock, 1)
        _project(session, clock, [*events, *large])

        # Act
        result = watch_payment(session, clock, events[-1])

        # Assert
        assert result is None
        assert _counts(session) == (0, 0, 0, 0)


def test_unknown_card_identity_still_uses_the_active_count_and_share_thresholds(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst(cards=None)
    with session_factory.begin() as session:
        _append(session, clock, 1)
        _project(session, clock, events)

        # Act
        result = watch_payment(session, clock, events[-1])

        # Assert
        (alert,) = _rows(session, "fraud_alerts")
        assert result == alert["alert_id"]
        assert cast(dict[str, object], alert["features"])["distinct_cards"] is None
        assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, result)) == 1
        assert _counts(session) == (1, 1, 1, 1)


@pytest.mark.parametrize(("cards", "large_count"), [(1, 0), (4, 4)])
def test_hand_edited_looser_rule_cannot_override_the_episode_baseline_gate(
    session_factory: sessionmaker[Session], clock: Clock, cards: int, large_count: int
) -> None:
    # Arrange: the episode is included in analysis but fails the shipped card/share baseline.
    events = _burst(cards=cards)
    large = [_event(500 + i, BASE + timedelta(seconds=i * 10 + 5), amount=5000) for i in range(large_count)]
    with session_factory.begin() as session:
        append_rule_version(session, Scope(MERCHANT), clock, 0, (1, 0.0, 1), "test:hand-edit", None)
        _project(session, clock, [*events, *large])

        # Act
        result = watch_payment(session, clock, events[-1])

        # Assert
        assert result is None
        assert _counts(session) == (0, 0, 0, 0)
        assert _rows(session, "ops_alert_deliveries") == []


def test_strict_rule_for_one_merchant_does_not_change_another_merchants_burst(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    own, other = _burst(), _burst(first=100, merchant=OTHER)
    with session_factory.begin() as session:
        _append(session, clock, 2)
        _project(session, clock, [*own, *other])

        # Act
        suppressed = watch_payment(session, clock, own[-1])
        raised = watch_payment(session, clock, other[-1])

        # Assert
        assert suppressed is None
        (alert,) = _rows(session, "fraud_alerts")
        assert (raised, alert["merchant_id"]) == (alert["alert_id"], OTHER)
        assert _rows(session, "fraud_alert_rule_versions") == [
            {"alert_id": raised, "merchant_id": OTHER, "rule_version": 0, "applied_at": clock()}
        ]
        assert [t["merchant_id"] for t in _rows(session, "tickets")] == [OTHER]
        assert [a["merchant_id"] for a in _rows(session, "ops_alerts")] == [OTHER]
        assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, raised)) is None
        assert applied_rule_version(session, Scope(OTHER), cast(uuid.UUID, raised)) == 0


def test_repeat_evaluation_upserts_version_and_time_without_recreating_side_effects(
    session_factory: sessionmaker[Session], clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    events = _burst(count=13)
    later_clock = _fixed_clock(clock() + timedelta(hours=1))
    caplog.set_level(logging.INFO, logger="ops_ai.fraud_watch.watch")
    with session_factory.begin() as session:
        _project(session, clock, events[:12])
        first_id = watch_payment(session, clock, events[11])
        before = [_rows(session, table) for table in ("tickets", "ops_alerts", "ops_alert_deliveries")]
        _append(session, later_clock, 1)
        _project(session, later_clock, events[12:])

        # Act
        second_id = watch_payment(session, later_clock, events[-1])
        replayed_id = watch_payment(session, later_clock, events[-1])

        # Assert
        (alert,) = _rows(session, "fraud_alerts")
        assert (first_id, second_id, replayed_id) == (alert["alert_id"],) * 3
        assert (alert["payment_count"], alert["last_seen_at"], alert["created_at"]) == (
            13,
            BASE + timedelta(seconds=120),
            clock(),
        )
        assert _rows(session, "fraud_alert_rule_versions") == [
            {"alert_id": first_id, "merchant_id": MERCHANT, "rule_version": 1, "applied_at": later_clock()}
        ]
        assert [
            _rows(session, table) for table in ("tickets", "ops_alerts", "ops_alert_deliveries")
        ] == before
        assert [r.getMessage() for r in caplog.records if "fraud alert raised" in r.getMessage()] == [
            f"fraud alert raised alert_id={first_id} merchant_id={MERCHANT} payment_count=12"
        ]


def test_same_version_repeat_refreshes_applied_at_without_adding_a_second_record(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    later_clock = _fixed_clock(clock() + timedelta(hours=1))
    with session_factory.begin() as session:
        _project(session, clock, events)
        first = watch_payment(session, clock, events[-1])

        # Act
        second = watch_payment(session, later_clock, events[-1])

        # Assert
        assert second == first
        assert _counts(session) == (1, 1, 1, 1)
        assert _rows(session, "fraud_alert_rule_versions") == [
            {"alert_id": first, "merchant_id": MERCHANT, "rule_version": 0, "applied_at": later_clock()}
        ]


def test_duplicate_ingest_delivery_does_not_reevaluate_the_applied_rule(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, clock, event)
        before = _rows(session, "fraud_alert_rule_versions")
        _append(session, clock, 2)

        # Act
        delivered = apply_payment_event(session, _fixed_clock(clock() + timedelta(hours=1)), events[-1])

        # Assert
        assert delivered is False
        assert _rows(session, "fraud_alert_rule_versions") == before
        assert [row["rule_version"] for row in before] == [0]
        assert _counts(session) == (1, 1, 1, 1)


def test_applied_rule_version_is_none_for_unknown_and_legacy_unrecorded_alerts(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    unknown = uuid.UUID(int=999_999)
    with session_factory.begin() as session:
        _project(session, clock, events)
        alert_id = watch_payment(session, clock, events[-1])
        session.execute(
            text("DELETE FROM ops_ai.fraud_alert_rule_versions WHERE alert_id = :id"), {"id": alert_id}
        )

        # Act
        missing = applied_rule_version(session, Scope(MERCHANT), unknown)
        legacy = applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, alert_id))

        # Assert
        assert (missing, legacy) == (None, None)
        assert _counts(session) == (1, 1, 1, 0)


@pytest.mark.parametrize(("origin", "amount"), [("backfill", 50), ("live", 101)])
def test_live_micro_gate_remains_required_even_with_an_active_version(
    session_factory: sessionmaker[Session], clock: Clock, origin: Origin, amount: int
) -> None:
    # Arrange
    events = _burst()
    gated = events[-1].model_copy(update={"origin": origin, "amount_minor": amount})
    with session_factory.begin() as session:
        _append(session, clock, 1)
        _project(session, clock, events)

        # Act
        result = watch_payment(session, clock, gated)

        # Assert
        assert result is None
        assert _counts(session) == (0, 0, 0, 0)


def test_analysis_window_remains_exactly_thirty_minutes_and_one_microsecond_inclusive_end(
    session_factory: sessionmaker[Session], clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    event = _burst()[-1]
    calls: list[tuple[Session, Scope, datetime, datetime]] = []

    def analyze(session: Session, scope: Scope, start: datetime, end: datetime) -> FraudAnalysis:
        calls.append((session, scope, start, end))
        return FraudAnalysis(start=start, end=end, micro_payments=0, episodes=())

    monkeypatch.setattr("ops_ai.fraud_watch.watch.analyze_fraud", analyze)
    with session_factory.begin() as session:
        _append(session, clock, 1)

        # Act
        result = watch_payment(session, clock, event)

        # Assert
        assert result is None
        assert calls == [
            (
                session,
                Scope(MERCHANT),
                event.occurred_at - timedelta(minutes=30),
                event.occurred_at + timedelta(microseconds=1),
            )
        ]
        assert _counts(session) == (0, 0, 0, 0)


def test_only_the_episode_containing_the_event_can_trigger_an_alert(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange: an older burst passes step 1; the event's separate, three-card episode fails it.
    older = _burst()
    current = _burst(first=100, start=BASE + timedelta(minutes=10), cards=3)
    with session_factory.begin() as session:
        _append(session, clock, 1)
        _project(session, clock, [*older, *current])

        # Act
        result = watch_payment(session, clock, current[-1])

        # Assert
        assert result is None
        assert _counts(session) == (0, 0, 0, 0)


def test_event_absent_from_any_episode_does_not_use_another_flagged_episode(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    isolated = _event(100, BASE + timedelta(minutes=10))
    with session_factory.begin() as session:
        _append(session, clock, 1)
        _project(session, clock, [*events, isolated])

        # Act
        result = watch_payment(session, clock, isolated)

        # Assert
        assert result is None
        assert _counts(session) == (0, 0, 0, 0)


def test_applied_version_and_all_alert_side_effects_roll_back_together(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    events = _burst()
    with session_factory.begin() as session:
        _project(session, clock, events)
    with session_factory() as session:
        # Act
        result = watch_payment(session, clock, events[-1])
        pending_counts = _counts(session)
        session.rollback()

    # Assert
    assert pending_counts == (1, 1, 1, 1)
    with session_factory() as session:
        assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, result)) is None
        assert _counts(session) == (0, 0, 0, 0)
        assert _rows(session, "ops_alert_deliveries") == []


def test_tightened_rule_suppresses_refresh_without_rewriting_existing_alert_or_applied_version(
    session_factory: sessionmaker[Session], clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    events = _burst()
    tables = ("fraud_alerts", "tickets", "ops_alerts", "ops_alert_deliveries", "fraud_alert_rule_versions")
    caplog.set_level(logging.INFO, logger="ops_ai.fraud_watch.watch")
    with session_factory.begin() as session:
        _project(session, clock, events)
        first = watch_payment(session, clock, events[-1])
        before = [_rows(session, table) for table in tables]
        _append(session, clock, 2)
        caplog.clear()

        # Act
        suppressed = watch_payment(session, _fixed_clock(clock() + timedelta(hours=1)), events[-1])

        # Assert
        assert suppressed is None
        assert [_rows(session, table) for table in tables] == before
        assert applied_rule_version(session, Scope(MERCHANT), cast(uuid.UUID, first)) == 0
        assert [r.getMessage() for r in caplog.records if "fraud alert raised" in r.getMessage()] == []


def test_sq3_eval_golden_is_unchanged_and_seeding_writes_no_rule_versions(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    anchor = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    log = generate(seed=42)
    question = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"

    # Act
    seed_dataset(session_factory, clock, log, anchor)
    with session_factory() as session:
        result = investigate_fraud(session, Scope(merchant_uuid("mch_0002")), question, anchor, MockLlm())

    # Assert
    assert {"intent": result.intent, "cause_label": result.cause_label} == {
        "intent": "card_testing",
        "cause_label": "card_testing",
    }
    assert [call.name for call in result.tool_calls] == ["analyze_fraud"]
    assert result.alerts == ()
    analysis = cast(FraudAnalysis, result.analysis)
    flagged = [episode for episode in analysis.episodes if episode.flagged]
    assert [set(episode.payment_ids) for episode in flagged] == [set(log.planted.sq3_transaction_ids)]
    assert [
        (episode.micro_payments, episode.distinct_cards, episode.decline_ratio, episode.reasons)
        for episode in flagged
    ] == [(40, None, 1.0, ("micro_burst", "card_identity_unknown", "high_decline_ratio"))]
    assert {item.ref.split(":", 1)[0] for item in result.evidence} == {"episode", "payment"}
    with session_factory() as session:
        assert _rows(session, "fraud_rule_versions") == []
        assert _rows(session, "fraud_rule_version_actors") == []
        assert _counts(session) == (0, 0, 0, 0)
