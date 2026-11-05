"""Fraud prediction accuracy against the planted labels (docs/specs/evaluation-judge.md section 2.1)."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from ops_ai.evaluation.classification import BinaryReport, binary_report
from ops_ai.fraud_watch.analysis import analyze_fraud
from ops_ai.scope import Scope
from ops_ai.seed import merchant_uuid, seed_shift, to_payment_event
from scripts.generate_dataset import ScenarioLog

CARD_TESTING_SCENARIO = "sq3_card_testing"


def card_testing_labels(log: ScenarioLog) -> frozenset[uuid.UUID]:
    return frozenset(
        tx.transaction_id
        for tx in log.transactions
        if tx.scenario == CARD_TESTING_SCENARIO and tx.merchant_id is not None and tx.payment_method == "card"
    )


def seeded_card_payment_ids(log: ScenarioLog, anchor: datetime) -> frozenset[uuid.UUID]:
    shift = seed_shift(log.planted, anchor)
    return frozenset(
        tx.transaction_id
        for tx in log.transactions
        if tx.timestamp + shift <= anchor and to_payment_event(tx, shift) is not None
    )


def fraud_prediction_report(
    session_factory: sessionmaker[Session], log: ScenarioLog, anchor: datetime
) -> BinaryReport:
    """Scores analyze_fraud's flagged episodes against the planted card-testing burst, over every merchant.

    Assumes seed_dataset(session_factory, clock, log, anchor) already ran.
    """
    shift = seed_shift(log.planted, anchor)
    start = min(tx.timestamp for tx in log.transactions) + shift
    end = anchor + timedelta(seconds=1)
    merchants = sorted({tx.merchant_id for tx in log.transactions if tx.merchant_id is not None})
    predicted: set[uuid.UUID] = set()
    with session_factory() as session:
        for merchant in merchants:
            analysis = analyze_fraud(session, Scope(merchant_id=merchant_uuid(merchant)), start, end)
            for episode in analysis.episodes:
                if episode.flagged:
                    predicted.update(episode.payment_ids)
    universe = seeded_card_payment_ids(log, anchor)
    return binary_report(frozenset(predicted & universe), card_testing_labels(log) & universe, universe)
