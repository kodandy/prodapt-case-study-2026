"""Golden test for investigate_transfer over the generated dataset (docs/specs/sq2.md section 7, Golden)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.db import make_session_factory
from ops_ai.scope import UserScope
from ops_ai.seed import seed_shift
from ops_ai.transfers import classify_transfer, get_payment_status
from ops_ai.transfers.seed import seed_transfers
from ops_ai.transfers.service import TransferInvestigation, investigate_transfer
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import DATA_SCHEMAS
from scripts.generate_dataset import CanonicalTransaction, ScenarioLog, generate

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
QUESTION = "My international transfer was declined even though I had enough money - what triggered it?"
XB_RULE = "xb_limit_kyc_tier1_v3"


@dataclass(frozen=True)
class Golden:
    session_factory: sessionmaker[Session]
    log: ScenarioLog
    planted: CanonicalTransaction
    inserted: int


@pytest.fixture(scope="module")
def golden(engine: Engine) -> Golden:
    """Seeds generate(seed=42) once per module; function-scoped fixtures of other modules truncate it."""
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
    log = generate(seed=42)
    inserted = seed_transfers(session_factory, log, ANCHOR)
    planted = next(tx for tx in log.transactions if tx.transaction_id == log.planted.sq2_transaction_id)
    return Golden(session_factory=session_factory, log=log, planted=planted, inserted=inserted)


def _investigate(
    golden: Golden, user_id: uuid.UUID, transfer_id: uuid.UUID | None = None
) -> TransferInvestigation | None:
    with golden.session_factory() as session:
        return investigate_transfer(session, UserScope(user_id=user_id), QUESTION, transfer_id, MockLlm())


def _planted_investigation(golden: Golden) -> TransferInvestigation:
    result = _investigate(golden, golden.planted.user_id)
    assert isinstance(result, TransferInvestigation)
    return result


def test_golden_the_planted_sender_without_an_id_gets_the_planted_transfer(golden: Golden) -> None:
    # Arrange / Act
    result = _planted_investigation(golden)

    # Assert
    assert result.transfer.transfer_id == golden.log.planted.sq2_transaction_id


def test_golden_the_cause_is_the_cross_border_limit_for_the_kyc_tier_and_the_sender_can_fix_it(
    golden: Golden,
) -> None:
    # Arrange / Act
    result = _planted_investigation(golden)

    # Assert
    assert (result.cause_code, result.remediation_owner) == ("cross_border_limit_for_kyc_tier", "sender")


def test_golden_the_numbers_are_the_amount_the_limit_the_tier_and_the_balance(golden: Golden) -> None:
    # Arrange / Act
    result = _planted_investigation(golden)

    # Assert
    transfer = result.transfer
    assert (transfer.amount_minor, transfer.currency) == (180_000, "USD")
    assert (transfer.limit_minor, transfer.limit_currency, transfer.kyc_tier) == (100_000, "USD", 1)
    assert transfer.sender_balance_minor == 525_000
    assert result.balance_covered_amount is True


def test_golden_there_is_one_fix_and_it_names_kyc_tier_2(golden: Golden) -> None:
    # Arrange / Act
    result = _planted_investigation(golden)

    # Assert
    assert len(result.suggested_fixes) == 1
    assert "tier 2" in result.suggested_fixes[0].lower()
    assert "split" not in result.suggested_fixes[0].lower()


def test_golden_the_evidence_cites_the_transfer_the_balance_and_the_limit_of_that_id(golden: Golden) -> None:
    # Arrange
    transfer_id = golden.log.planted.sq2_transaction_id

    # Act
    result = _planted_investigation(golden)

    # Assert
    assert [item.ref for item in result.evidence] == [
        f"transfer:{transfer_id}",
        f"balance:{transfer_id}",
        f"limit:{transfer_id}",
    ]
    assert "1800.00 USD" in result.evidence[0].summary


def test_golden_the_tool_call_is_get_payment_status_for_the_latest_failed_transfer(golden: Golden) -> None:
    # Arrange / Act
    result = _planted_investigation(golden)

    # Assert
    assert [(call.name, call.args) for call in result.tool_calls] == [
        ("get_payment_status", {"transfer_id": "latest_failed"})
    ]


def test_golden_the_mock_llm_narrates_the_evidence_and_the_rule_id_stays_out(golden: Golden) -> None:
    # Arrange / Act
    result = _planted_investigation(golden)

    # Assert
    assert (result.mode, result.model) == ("llm", "mock")
    assert result.text is not None
    assert f"transfer:{golden.log.planted.sq2_transaction_id}" in result.text
    assert XB_RULE not in result.text
    assert XB_RULE not in repr(result)


def test_golden_asking_for_the_planted_id_explicitly_gives_the_same_result(golden: Golden) -> None:
    # Arrange
    planted_id = golden.log.planted.sq2_transaction_id

    # Act
    explicit = _investigate(golden, golden.planted.user_id, planted_id)

    # Assert
    assert explicit is not None
    assert explicit.transfer == _planted_investigation(golden).transfer
    assert [(call.name, call.args) for call in explicit.tool_calls] == [
        ("get_payment_status", {"transfer_id": str(planted_id)})
    ]


def test_golden_another_sender_asking_for_the_planted_id_gets_nothing(golden: Golden) -> None:
    # Arrange
    other_sender = next(
        tx.user_id
        for tx in golden.log.transactions
        if tx.record_kind == "p2p_transfer" and tx.user_id != golden.planted.user_id
    )

    # Act
    result = _investigate(golden, other_sender, golden.log.planted.sq2_transaction_id)

    # Assert
    assert result is None


def test_golden_the_seeded_transfer_count_is_the_p2p_records_at_or_before_the_anchor(golden: Golden) -> None:
    # Arrange
    shift = seed_shift(golden.log.planted, ANCHOR)
    expected = sum(
        1
        for tx in golden.log.transactions
        if tx.record_kind == "p2p_transfer" and tx.timestamp + shift <= ANCHOR
    )

    # Act
    inserted = golden.inserted

    # Assert
    assert expected > 100
    assert inserted == expected


def test_golden_every_seeded_failed_transfer_is_classified_as_the_record_says(golden: Golden) -> None:
    # Arrange
    shift = seed_shift(golden.log.planted, ANCHOR)
    failed = [
        tx
        for tx in golden.log.transactions
        if tx.record_kind == "p2p_transfer" and tx.status == "failed" and tx.timestamp + shift <= ANCHOR
    ]

    # Act
    observed: dict[uuid.UUID, tuple[str, str]] = {}
    with golden.session_factory() as session:
        for tx in failed:
            status = get_payment_status(session, UserScope(user_id=tx.user_id), tx.transaction_id)
            assert status is not None
            observed[tx.transaction_id] = classify_transfer(status)

    # Assert
    for tx in failed:
        short_balance = tx.sender_balance_minor is not None and tx.sender_balance_minor < tx.amount_minor
        if tx.rule_id == "kyc_gate_v1":
            expected = ("kyc_verification_failed", "sender")
        elif tx.rule_id == XB_RULE:
            expected = ("cross_border_limit_for_kyc_tier", "sender")
        elif tx.failure_reason == "Insufficient balance" and short_balance:
            expected = ("insufficient_funds", "sender")
        else:
            expected = ("unclassified", "unknown")
        assert observed[tx.transaction_id] == expected
    assert len(failed) > 10
