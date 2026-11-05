"""Live tail of seed_dataset, from docs/specs/sq3.md "Feedback-adjusted thresholds ... (11-06)" S-1."""

from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.seed import merchant_uuid, seed_dataset, to_payment_event
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import ScenarioLog, generate

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
LIVE_SINCE = ANCHOR - timedelta(hours=24)
SQ3_MERCHANT = "mch_0002"
COUNT_QUERIES = (
    text("SELECT count(*) FROM ops_ai.fraud_alerts"),
    text("SELECT count(*) FROM ops_ai.tickets"),
    text("SELECT count(*) FROM ops_ai.ops_alerts"),
    text("SELECT count(*) FROM ops_ai.fraud_alert_rule_versions"),
)
ALERT_QUERY = (
    "SELECT alert_id, merchant_id, kind, first_payment_id, payment_count, features FROM ops_ai.fraud_alerts"
)


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate(seed=42)


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _counts(session_factory: sessionmaker[Session]) -> tuple[int, int, int, int]:
    with session_factory() as session:
        counts = [int(session.execute(query).scalar_one()) for query in COUNT_QUERIES]
    return counts[0], counts[1], counts[2], counts[3]


def test_to_payment_event_stamps_the_requested_origin_and_keeps_every_other_field(log: ScenarioLog) -> None:
    # Arrange
    tx = next(tx for tx in log.transactions if tx.transaction_id in set(log.planted.sq3_transaction_ids))
    shift = timedelta(hours=1)

    # Act
    backfill = to_payment_event(tx, shift)
    live = to_payment_event(tx, shift, "live")

    # Assert
    assert backfill is not None
    assert live is not None
    assert backfill.origin == "backfill"
    assert live.origin == "live"
    assert live.model_dump(exclude={"origin"}) == backfill.model_dump(exclude={"origin"})


def test_live_tail_raises_one_alert_and_one_fraud_ticket_for_the_planted_sq3_burst(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange
    planted = log.planted.sq3_transaction_ids
    merchant = merchant_uuid(SQ3_MERCHANT)

    # Act
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR, live_since=LIVE_SINCE)

    # Assert
    alerts = _rows(session_factory, ALERT_QUERY)
    assert len(alerts) == 1
    (alert,) = alerts
    assert alert["merchant_id"] == merchant
    assert alert["kind"] == "card_testing"
    assert alert["first_payment_id"] == planted[0]
    assert alert["payment_count"] == len(planted) == 40
    features = alert["features"]
    assert isinstance(features, dict)
    assert features["distinct_cards"] is None
    tickets = _rows(
        session_factory, "SELECT queue, priority, status, merchant_id, alert_id FROM ops_ai.tickets"
    )
    assert tickets == [
        {
            "queue": "fraud",
            "priority": "high",
            "status": "open",
            "merchant_id": merchant,
            "alert_id": alert["alert_id"],
        }
    ]


def test_live_tail_records_the_baseline_rule_version_and_one_ops_alert(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange / Act
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR, live_since=LIVE_SINCE)

    # Assert
    applied = _rows(session_factory, "SELECT merchant_id, rule_version FROM ops_ai.fraud_alert_rule_versions")
    assert applied == [{"merchant_id": merchant_uuid(SQ3_MERCHANT), "rule_version": 0}]
    assert _counts(session_factory) == (1, 1, 1, 1)


def test_live_tail_is_idempotent_on_a_second_run(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange
    first = seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR, live_since=LIVE_SINCE)

    # Act
    second = seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR, live_since=LIVE_SINCE)

    # Assert
    assert first.payments_applied > 0
    assert second.payments_applied == 0
    assert _counts(session_factory) == (1, 1, 1, 1)


def test_live_since_after_the_burst_keeps_it_backfill_and_raises_no_alert(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange / Act
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR, live_since=ANCHOR)

    # Assert
    assert _counts(session_factory) == (0, 0, 0, 0)


def test_live_tail_seeds_the_same_payment_facts_as_a_backfill_seed(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange
    query = "SELECT payment_id, merchant_id, amount_minor, state, occurred_at FROM ops_ai.payment_facts"
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR)
    backfill = sorted(_rows(session_factory, query), key=lambda row: str(row["payment_id"]))
    with session_factory.begin() as session:
        session.execute(text("TRUNCATE ops_ai.payment_facts, ops_ai.processed_events CASCADE"))

    # Act
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR, live_since=LIVE_SINCE)

    # Assert
    live = sorted(_rows(session_factory, query), key=lambda row: str(row["payment_id"]))
    assert len(live) == len(backfill) > 0
    assert live == backfill
