"""Tests for ops_ai.seed.main, written from docs/specs/sq1.md section 4 (python -m ops_ai.seed)."""

import re
from datetime import UTC, datetime

import pytest
from ops_ai.seed import main
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import generate, to_export_row

ANCHOR_ISO = "2026-11-03T12:00:00+00:00"
ANCHOR = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
RECORDS = 1000
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _expected_counts(seed: int) -> tuple[int, int, int]:
    """Applied / skipped / incidents computed straight from the generated log."""
    log = generate(seed=seed, records=RECORDS)
    window_end = log.planted.sq1_window_end
    kept = [tx for tx in log.transactions if tx.timestamp <= window_end]
    applied = sum(1 for tx in kept if tx.record_kind == "merchant_payment" and tx.payment_method == "card")
    skipped = len(log.transactions) - len(kept)
    return applied, skipped, len(log.incidents)


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str]:
    code = main(argv)
    return code, capsys.readouterr().out


def _count_rows(session_factory: sessionmaker[Session], table: str) -> int:
    queries = {
        "payment_facts": "SELECT count(*) FROM ops_ai.payment_facts",
        "incidents": "SELECT count(*) FROM ops_ai.incidents",
    }
    with session_factory() as session:
        return int(session.execute(text(queries[table])).scalar_one())


def test_main_returns_zero_and_seeds_the_database(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    applied, _skipped, incidents = _expected_counts(42)

    # Act
    code, _out = _run(capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO])

    # Assert
    assert code == 0
    assert _count_rows(session_factory, "payment_facts") == applied
    assert _count_rows(session_factory, "incidents") == incidents


def test_main_prints_one_line_carrying_the_three_counts_in_seed_result_order(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    applied, skipped, incidents = _expected_counts(42)

    # Act
    _code, out = _run(capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO])

    # Assert
    assert len(out.strip().splitlines()) == 1
    assert re.findall(r"\d+", out) == [str(applied), str(skipped), str(incidents)]


def test_main_prints_no_merchant_data_beyond_counts(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    log = generate(seed=42, records=RECORDS)
    merchant_ids = {
        str(row["merchant_id"]) for row in map(to_export_row, log.transactions) if row["merchant_id"]
    }

    # Act
    _code, out = _run(capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO])

    # Assert
    assert [m for m in merchant_ids if m in out] == []
    assert UUID_PATTERN.findall(out) == []
    assert "mch_" not in out


def test_main_second_run_reports_zero_applied_and_leaves_the_rows_unchanged(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    argv = ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO]
    _run(capsys, argv)
    facts_after_first = _count_rows(session_factory, "payment_facts")

    # Act
    code, out = _run(capsys, argv)

    # Assert
    assert code == 0
    assert re.findall(r"\d+", out)[0] == "0"
    assert _count_rows(session_factory, "payment_facts") == facts_after_first


def test_main_defaults_seed_to_42_and_records_to_12000(
    session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    log = generate()
    window_end = log.planted.sq1_window_end
    expected_applied = sum(
        1
        for tx in log.transactions
        if tx.timestamp <= window_end and tx.record_kind == "merchant_payment" and tx.payment_method == "card"
    )

    # Act
    code, out = _run(capsys, ["--anchor", ANCHOR_ISO])

    # Assert
    assert code == 0
    assert re.findall(r"\d+", out)[0] == str(expected_applied)
