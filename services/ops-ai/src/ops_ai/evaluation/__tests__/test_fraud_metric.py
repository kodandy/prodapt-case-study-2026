"""Fraud prediction accuracy against the planted labels (docs/specs/evaluation-judge.md section 2.1)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from ops_ai.db import make_session_factory
from ops_ai.evaluation.fraud import card_testing_labels, fraud_prediction_report, seeded_card_payment_ids
from ops_ai.seed import SeedResult, seed_dataset, seed_shift
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import DATA_SCHEMAS
from scripts.generate_dataset import ScenarioLog, generate

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
SQ3_SERIES_SIZE = 40


@dataclass(frozen=True)
class Seeded:
    session_factory: sessionmaker[Session]
    log: ScenarioLog
    result: SeedResult


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate(seed=42)


@pytest.fixture(scope="module")
def seeded(engine: Engine, log: ScenarioLog) -> Seeded:
    """Seeds generate(seed=42) once per module; other modules function-scoped fixtures truncate it."""
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) AND tablename <> 'alembic_version'"
            ),
            {"schemas": list(DATA_SCHEMAS)},
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    session_factory = make_session_factory(engine)
    result = seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR)
    return Seeded(session_factory=session_factory, log=log, result=result)


def test_card_testing_labels_are_the_planted_sq3_transaction_ids(log: ScenarioLog) -> None:
    # Arrange
    expected = frozenset(log.planted.sq3_transaction_ids)

    # Act
    labels = card_testing_labels(log)

    # Assert
    assert labels == expected
    assert len(labels) == SQ3_SERIES_SIZE


def test_card_testing_labels_equal_the_merchant_card_payments_of_the_sq3_scenario(log: ScenarioLog) -> None:
    # Arrange
    expected = frozenset(
        tx.transaction_id
        for tx in log.transactions
        if tx.scenario == "sq3_card_testing" and tx.merchant_id is not None and tx.payment_method == "card"
    )

    # Act
    labels = card_testing_labels(log)

    # Assert
    assert labels == expected


def test_seeded_card_payment_ids_are_the_merchant_card_payments_up_to_the_anchor(log: ScenarioLog) -> None:
    # Arrange
    shift = seed_shift(log.planted, ANCHOR)
    expected: frozenset[uuid.UUID] = frozenset(
        tx.transaction_id
        for tx in log.transactions
        if tx.merchant_id is not None
        and tx.payment_method == "card"
        and tx.record_kind == "merchant_payment"
        and tx.timestamp + shift <= ANCHOR
    )
    transfer_ids = {tx.transaction_id for tx in log.transactions if tx.merchant_id is None}

    # Act
    ids = seeded_card_payment_ids(log, ANCHOR)

    # Assert
    assert ids == expected
    assert ids.isdisjoint(transfer_ids)


def test_seeded_card_payment_ids_contain_every_card_testing_label(log: ScenarioLog) -> None:
    # Arrange
    labels = card_testing_labels(log)

    # Act
    ids = seeded_card_payment_ids(log, ANCHOR)

    # Assert
    assert labels <= ids
    assert len(ids) > len(labels)


def test_fraud_prediction_report_finds_every_card_testing_payment_with_high_precision(seeded: Seeded) -> None:
    # Arrange
    assert seeded.result.payments_applied > 0

    # Act
    report = fraud_prediction_report(seeded.session_factory, seeded.log, ANCHOR)

    # Assert
    assert report.true_positive == SQ3_SERIES_SIZE
    assert report.false_negative == 0
    assert report.recall == 1.0
    assert report.precision >= 0.95
    assert report.f1 >= 0.95


def test_fraud_prediction_report_counts_add_up_to_the_seeded_card_payments(seeded: Seeded) -> None:
    # Arrange
    universe_size = len(seeded_card_payment_ids(seeded.log, ANCHOR))

    # Act
    report = fraud_prediction_report(seeded.session_factory, seeded.log, ANCHOR)

    # Assert
    total = report.true_positive + report.false_positive + report.false_negative + report.true_negative
    assert total == universe_size
    assert report.accuracy == pytest.approx((report.true_positive + report.true_negative) / universe_size)
