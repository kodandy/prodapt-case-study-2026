"""Live tail of python -m ops_ai.seed, from docs/specs/sq3.md S-3 (the seed CLI that make demo runs)."""

import pytest
from ops_ai.seed import main, merchant_uuid
from sqlalchemy import TextClause, text
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import generate

ANCHOR_ISO = "2026-11-03T15:00:00+00:00"
SQ3_MERCHANT = "mch_0002"
PLANTED_BURST_SIZE = 40
COUNT_QUERIES = (
    text("SELECT count(*) FROM ops_ai.fraud_alerts"),
    text("SELECT count(*) FROM ops_ai.tickets"),
    text("SELECT count(*) FROM ops_ai.ops_alerts"),
)
ALERT_QUERY = text(
    "SELECT alert_id, merchant_id, kind, first_payment_id, payment_count FROM ops_ai.fraud_alerts"
)
TICKET_QUERY = text("SELECT queue, priority, status, merchant_id, alert_id FROM ops_ai.tickets")
FACTS_QUERY = text(
    "SELECT payment_id, merchant_id, amount_minor, state, occurred_at FROM ops_ai.payment_facts"
)


def _counts(session_factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with session_factory() as session:
        counts = [int(session.execute(query).scalar_one()) for query in COUNT_QUERIES]
    return counts[0], counts[1], counts[2]


def _rows(session_factory: sessionmaker[Session], query: TextClause) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(query).mappings()]


def _facts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return sorted(_rows(session_factory, FACTS_QUERY), key=lambda row: str(row["payment_id"]))


def test_cli_default_live_tail_raises_one_alert_and_one_fraud_ticket_for_the_planted_burst(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    first_planted = generate(seed=42).planted.sq3_transaction_ids[0]
    merchant = merchant_uuid(SQ3_MERCHANT)

    # Act
    code = main(["--anchor", ANCHOR_ISO])

    # Assert
    assert code == 0
    alerts = _rows(session_factory, ALERT_QUERY)
    assert len(alerts) == 1
    (alert,) = alerts
    assert alert["merchant_id"] == merchant
    assert alert["kind"] == "card_testing"
    assert alert["first_payment_id"] == first_planted
    assert alert["payment_count"] == PLANTED_BURST_SIZE
    assert _rows(session_factory, TICKET_QUERY) == [
        {
            "queue": "fraud",
            "priority": "high",
            "status": "open",
            "merchant_id": merchant,
            "alert_id": alert["alert_id"],
        }
    ]
    assert _counts(session_factory) == (1, 1, 1)


def test_cli_live_tail_hours_zero_seeds_backfill_only_and_raises_nothing(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    argv = ["--anchor", ANCHOR_ISO, "--live-tail-hours", "0"]

    # Act
    code = main(argv)

    # Assert
    assert code == 0
    assert len(_facts(session_factory)) > 0
    assert _counts(session_factory) == (0, 0, 0)


def test_cli_rejects_a_negative_live_tail_with_exit_code_2_and_seeds_nothing(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    argv = ["--anchor", ANCHOR_ISO, "--live-tail-hours", "-1"]

    # Act
    with pytest.raises(SystemExit) as raised:
        main(argv)

    # Assert
    assert raised.value.code == 2
    assert _facts(session_factory) == []


def test_cli_second_run_with_the_live_tail_adds_no_alert_ticket_or_ops_alert(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    main(["--anchor", ANCHOR_ISO])
    capsys.readouterr()

    # Act
    code = main(["--anchor", ANCHOR_ISO])

    # Assert
    assert code == 0
    assert capsys.readouterr().out.startswith("seed: 0 payments applied")
    assert _counts(session_factory) == (1, 1, 1)


def test_cli_live_tail_seeds_the_same_payment_facts_as_a_backfill_only_run(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    main(["--anchor", ANCHOR_ISO, "--live-tail-hours", "0"])
    backfill = _facts(session_factory)
    with session_factory.begin() as session:
        session.execute(text("TRUNCATE ops_ai.payment_facts, ops_ai.processed_events CASCADE"))

    # Act
    main(["--anchor", ANCHOR_ISO])

    # Assert
    live = _facts(session_factory)
    assert len(live) == len(backfill) > 0
    assert live == backfill
