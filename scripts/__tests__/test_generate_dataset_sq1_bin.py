"""Tests for the SQ1 BIN change in scripts/generate_dataset.py, written from docs/specs/sq1.md section 2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scripts.generate_dataset import DEMO_DAY, SQ1_BIN, ScenarioLog, generate, to_export_row

Row = dict[str, Any]

SQ1_BIN_LITERAL = "400000"
WINDOW_FAILURE_REASON = "Issuer unavailable (timeout) for BIN 400000"


def _ts(row: Row) -> datetime:
    return datetime.fromisoformat(str(row["timestamp"]))


def _failure_rate(rows: list[Row]) -> float:
    return sum(1 for r in rows if r["status"] == "failed") / len(rows)


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate()


@pytest.fixture(scope="module")
def rows(log: ScenarioLog) -> list[Row]:
    return [to_export_row(tx) for tx in log.transactions]


@pytest.fixture(scope="module")
def window_rows(log: ScenarioLog, rows: list[Row]) -> list[Row]:
    p = log.planted
    return [
        r
        for r in rows
        if r["merchant_id"] == p.sq1_merchant_id
        and r["payment_method"] == "card"
        and p.sq1_window_start <= _ts(r) < p.sq1_window_end
    ]


def test_sq1_bin_constant_is_400000() -> None:
    # Arrange / Act
    value = SQ1_BIN

    # Assert
    assert value == SQ1_BIN_LITERAL


def test_planted_sq1_card_bin_is_400000(log: ScenarioLog) -> None:
    # Arrange / Act
    planted_bin = log.planted.sq1_card_bin

    # Assert
    assert planted_bin == SQ1_BIN_LITERAL


def test_sq1_window_still_has_at_least_200_card_records(window_rows: list[Row]) -> None:
    # Arrange / Act
    count = len(window_rows)

    # Assert
    assert count >= 200


def test_sq1_window_failure_rate_is_still_between_12_and_18_percent(window_rows: list[Row]) -> None:
    # Arrange / Act
    rate = _failure_rate(window_rows)

    # Assert
    assert 0.12 <= rate <= 0.18


def test_at_least_80_percent_of_sq1_window_failures_are_on_bin_400000(window_rows: list[Row]) -> None:
    # Arrange
    failures = [r for r in window_rows if r["status"] == "failed"]

    # Act
    on_bin = sum(1 for r in failures if r["card_bin"] == SQ1_BIN_LITERAL)

    # Assert
    assert failures != []
    assert on_bin / len(failures) >= 0.80


def test_every_window_failure_on_bin_400000_carries_the_issuer_unavailable_reason(
    window_rows: list[Row],
) -> None:
    # Arrange
    on_bin_failures = [r for r in window_rows if r["status"] == "failed" and r["card_bin"] == SQ1_BIN_LITERAL]

    # Act
    reasons = {r["failure_reason"] for r in on_bin_failures}

    # Assert
    assert on_bin_failures != []
    assert reasons == {WINDOW_FAILURE_REASON}


def test_sq1_baseline_failure_rate_in_the_seven_days_before_demo_day_is_still_below_5_percent(
    log: ScenarioLog, rows: list[Row]
) -> None:
    # Arrange
    demo_start = datetime(DEMO_DAY.year, DEMO_DAY.month, DEMO_DAY.day, tzinfo=UTC)
    baseline = [
        r
        for r in rows
        if r["merchant_id"] == log.planted.sq1_merchant_id
        and r["payment_method"] == "card"
        and demo_start - timedelta(days=7) <= _ts(r) < demo_start
    ]

    # Act
    rate = _failure_rate(baseline)

    # Assert
    assert baseline != []
    assert rate < 0.05


def test_historical_incident_inc_0001_names_bin_400000_and_the_acq_b_reroute(log: ScenarioLog) -> None:
    # Arrange
    incident = next(i for i in log.incidents if i.incident_id == "inc_0001")

    # Act
    mentions = (
        "BIN 400000" in incident.title,
        "BIN 400000" in incident.root_cause,
        "BIN 400000" in incident.fix_applied,
        "acq_b" in incident.fix_applied,
    )

    # Assert
    assert mentions == (True, True, True, True)
    assert incident.resolved_at is not None
    assert incident.resolved_at > incident.started_at
