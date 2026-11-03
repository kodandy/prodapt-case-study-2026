"""Tests for seed_transfers and python -m ops_ai.transfers, from docs/specs/sq2.md section 4."""

import re
import runpy
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.seed import seed_shift
from ops_ai.transfers.seed import seed_transfers
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import (
    CanonicalTransaction,
    PaymentMethod,
    PlantedScenarios,
    RecordKind,
    ScenarioLog,
    Status,
    generate,
)

WINDOW_END = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
ANCHOR = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
ANCHOR_ISO = "2026-11-03T12:00:00+00:00"
XB_REASON = "Cross-border per-transaction limit exceeded for KYC tier 1 (1800.00 USD > 1000.00 USD)"
SHIFT = ANCHOR - WINDOW_END
RECORDS = 1000
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

FACTS_QUERY = (
    "SELECT transfer_id, sender_user_id, amount_minor, currency, method, status, country_sender, "
    "country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, next_tier_limit_minor, "
    "sender_balance_minor, decided_at, ingest_source FROM ops_ai.transfer_facts ORDER BY transfer_id"
)


def _tx(
    n: int,
    *,
    record_kind: RecordKind = "p2p_transfer",
    status: Status = "failed",
    failure_reason: str | None = "Insufficient balance",
    rule_id: str | None = None,
    sender_balance_minor: int | None = 40_000,
    amount_minor: int = 180_000,
    payment_method: PaymentMethod = "bank_transfer",
    merchant_id: str | None = None,
    country_receiver: str = "IN",
    timestamp: datetime = WINDOW_END - timedelta(hours=1),
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=uuid.UUID(int=n),
        user_id=uuid.UUID(int=1000 + n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency="USD",
        payment_method=payment_method,
        status=status,
        failure_reason=failure_reason,
        fraud_score=0.1,
        timestamp=timestamp,
        chargeback_flag=False,
        settlement_status="pending",
        resolution_notes=None,
        kyc_status="verified",
        country_sender="US",
        country_receiver=country_receiver,
        record_kind=record_kind,
        scenario="baseline",
        card_bin=None,
        acquirer=None,
        rule_id=rule_id,
        sender_balance_minor=sender_balance_minor,
    )


def _log(transactions: tuple[CanonicalTransaction, ...]) -> ScenarioLog:
    planted = PlantedScenarios(
        sq1_merchant_id="mch_0001",
        sq1_window_start=WINDOW_END - timedelta(hours=6),
        sq1_window_end=WINDOW_END,
        sq1_card_bin="400000",
        sq2_transaction_id=uuid.UUID(int=2),
        sq3_merchant_id="mch_0002",
        sq3_transaction_ids=(uuid.UUID(int=3),),
        sq4_merchant_id="mch_0003",
    )
    return ScenarioLog(transactions=transactions, incidents=(), planted=planted)


def _rows(
    session_factory: sessionmaker[Session], query: str = FACTS_QUERY, **params: object
) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session]) -> int:
    return len(_rows(session_factory, "SELECT transfer_id FROM ops_ai.transfer_facts"))


def _run_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str]
) -> tuple[int | str | None, str]:
    monkeypatch.setattr(sys, "argv", ["python -m ops_ai.transfers", *args])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("ops_ai.transfers", run_name="__main__", alter_sys=True)
    return exc.value.code, capsys.readouterr().out


# --- seed_transfers --------------------------------------------------------------------------------------


def test_seed_transfers_inserts_each_p2p_transfer_with_the_decision_fields_and_shifted_time(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    limit_case = _tx(
        10,
        failure_reason=XB_REASON,
        rule_id="xb_limit_kyc_tier1_v3",
        sender_balance_minor=525_000,
    )
    funds_case = _tx(11, sender_balance_minor=40_000, country_receiver="US")
    success_case = _tx(12, status="success", failure_reason=None, sender_balance_minor=900_000)
    log = _log((limit_case, funds_case, success_case))

    # Act
    inserted = seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 3
    assert _rows(session_factory) == [
        {
            "transfer_id": limit_case.transaction_id,
            "sender_user_id": limit_case.user_id,
            "amount_minor": 180_000,
            "currency": "USD",
            "method": "bank_transfer",
            "status": "failed",
            "country_sender": "US",
            "country_receiver": "IN",
            "reason_code": "cross_border_limit_kyc_tier",
            "rule_id": "xb_limit_kyc_tier1_v3",
            "kyc_tier": 1,
            "limit_minor": 100_000,
            "limit_currency": "USD",
            "next_tier_limit_minor": 1_000_000,
            "sender_balance_minor": 525_000,
            "decided_at": limit_case.timestamp + SHIFT,
            "ingest_source": "seed",
        },
        {
            "transfer_id": funds_case.transaction_id,
            "sender_user_id": funds_case.user_id,
            "amount_minor": 180_000,
            "currency": "USD",
            "method": "bank_transfer",
            "status": "failed",
            "country_sender": "US",
            "country_receiver": "US",
            "reason_code": "insufficient_funds",
            "rule_id": None,
            "kyc_tier": None,
            "limit_minor": None,
            "limit_currency": None,
            "next_tier_limit_minor": None,
            "sender_balance_minor": 40_000,
            "decided_at": funds_case.timestamp + SHIFT,
            "ingest_source": "seed",
        },
        {
            "transfer_id": success_case.transaction_id,
            "sender_user_id": success_case.user_id,
            "amount_minor": 180_000,
            "currency": "USD",
            "method": "bank_transfer",
            "status": "success",
            "country_sender": "US",
            "country_receiver": "IN",
            "reason_code": None,
            "rule_id": None,
            "kyc_tier": None,
            "limit_minor": None,
            "limit_currency": None,
            "next_tier_limit_minor": None,
            "sender_balance_minor": 900_000,
            "decided_at": success_case.timestamp + SHIFT,
            "ingest_source": "seed",
        },
    ]


def test_seed_transfers_skips_merchant_payments(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer = _tx(10)
    card = _tx(
        20, record_kind="merchant_payment", merchant_id="mch_0001", payment_method="card", failure_reason=None
    )
    wallet = _tx(
        21,
        record_kind="merchant_payment",
        merchant_id="mch_0002",
        payment_method="wallet",
        status="success",
        failure_reason=None,
    )
    log = _log((transfer, card, wallet))

    # Act
    inserted = seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 1
    assert [row["transfer_id"] for row in _rows(session_factory)] == [transfer.transaction_id]


def test_seed_transfers_keeps_a_transfer_exactly_at_the_anchor_and_skips_one_second_after(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    at_anchor = _tx(10, timestamp=WINDOW_END)
    before = _tx(11, timestamp=WINDOW_END - timedelta(seconds=1))
    after = _tx(12, timestamp=WINDOW_END + timedelta(seconds=1))
    much_later = _tx(13, timestamp=WINDOW_END + timedelta(hours=2))
    log = _log((at_anchor, before, after, much_later))

    # Act
    inserted = seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 2
    assert {row["transfer_id"] for row in _rows(session_factory)} == {
        at_anchor.transaction_id,
        before.transaction_id,
    }


def test_seed_transfers_uses_the_same_shift_as_the_payment_seed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    tx = _tx(10, timestamp=WINDOW_END - timedelta(minutes=30))
    log = _log((tx,))
    shift = seed_shift(log.planted, ANCHOR)

    # Act
    seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert _rows(session_factory)[0]["decided_at"] == tx.timestamp + shift == ANCHOR - timedelta(minutes=30)


def test_seed_transfers_second_run_returns_zero_and_leaves_the_rows_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((_tx(10), _tx(11, status="success", failure_reason=None)))
    seed_transfers(session_factory, log, ANCHOR)
    after_first = _rows(session_factory)

    # Act
    second = seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert second == 0
    assert _rows(session_factory) == after_first


def test_seed_transfers_a_re_run_with_a_later_anchor_keeps_the_first_rows_and_adds_nothing_for_them(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((_tx(10),))
    seed_transfers(session_factory, log, ANCHOR)
    after_first = _rows(session_factory)

    # Act
    second = seed_transfers(session_factory, log, ANCHOR + timedelta(days=1))

    # Assert
    assert second == 0
    assert _rows(session_factory) == after_first


def test_seed_transfers_with_no_p2p_transfer_returns_zero_and_writes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((_tx(20, record_kind="merchant_payment", merchant_id="mch_0001", payment_method="card"),))

    # Act
    inserted = seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 0
    assert _count(session_factory) == 0


def test_seed_transfers_stores_the_planted_sq2_transfer_from_the_generated_dataset(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = generate(seed=42, records=RECORDS)
    planted = next(tx for tx in log.transactions if tx.transaction_id == log.planted.sq2_transaction_id)

    # Act
    seed_transfers(session_factory, log, ANCHOR)

    # Assert
    rows = _rows(
        session_factory,
        "SELECT * FROM ops_ai.transfer_facts WHERE transfer_id = :transfer_id",
        transfer_id=planted.transaction_id,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["sender_user_id"] == planted.user_id
    assert (row["amount_minor"], row["currency"]) == (180_000, "USD")
    assert (row["reason_code"], row["rule_id"]) == ("cross_border_limit_kyc_tier", "xb_limit_kyc_tier1_v3")
    assert (row["kyc_tier"], row["limit_minor"], row["limit_currency"]) == (1, 100_000, "USD")
    assert (row["next_tier_limit_minor"], row["sender_balance_minor"]) == (1_000_000, 525_000)
    assert row["decided_at"] == planted.timestamp + seed_shift(log.planted, ANCHOR)


def test_seed_transfers_count_equals_the_p2p_records_of_the_log_at_or_before_the_anchor(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = generate(seed=42, records=RECORDS)
    shift = seed_shift(log.planted, ANCHOR)
    expected = sum(
        1 for tx in log.transactions if tx.record_kind == "p2p_transfer" and tx.timestamp + shift <= ANCHOR
    )

    # Act
    inserted = seed_transfers(session_factory, log, ANCHOR)

    # Assert
    assert expected > 0
    assert inserted == expected
    assert _count(session_factory) == expected
    assert {row["ingest_source"] for row in _rows(session_factory)} == {"seed"}


# --- python -m ops_ai.transfers --------------------------------------------------------------------------


def _expected_count(seed: int, records: int) -> int:
    log = generate(seed=seed, records=records)
    shift = seed_shift(log.planted, ANCHOR)
    return sum(
        1 for tx in log.transactions if tx.record_kind == "p2p_transfer" and tx.timestamp + shift <= ANCHOR
    )


def test_main_exits_zero_and_seeds_the_database(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    expected = _expected_count(42, RECORDS)

    # Act
    code, _out = _run_main(
        monkeypatch, capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO]
    )

    # Assert
    assert code == 0
    assert _count(session_factory) == expected


def test_main_prints_one_line_with_the_inserted_count_only(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    expected = _expected_count(42, RECORDS)

    # Act
    _code, out = _run_main(
        monkeypatch, capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO]
    )

    # Assert
    assert len(out.strip().splitlines()) == 1
    assert re.findall(r"\d+", out) == [str(expected)]


def test_main_prints_no_user_or_transfer_ids(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    log = generate(seed=42, records=RECORDS)
    user_ids = {str(tx.user_id) for tx in log.transactions}

    # Act
    _code, out = _run_main(
        monkeypatch, capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO]
    )

    # Assert
    assert [user_id for user_id in user_ids if user_id in out] == []
    assert UUID_PATTERN.findall(out) == []


def test_main_second_run_prints_zero_and_leaves_the_rows_unchanged(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    args = ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO]
    _run_main(monkeypatch, capsys, args)
    after_first = _count(session_factory)

    # Act
    code, out = _run_main(monkeypatch, capsys, args)

    # Assert
    assert code == 0
    assert re.findall(r"\d+", out) == ["0"]
    assert _count(session_factory) == after_first


def test_main_defaults_seed_to_42_and_records_to_12000(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    expected = _expected_count(42, 12000)

    # Act
    code, out = _run_main(monkeypatch, capsys, ["--anchor", ANCHOR_ISO])

    # Assert
    assert code == 0
    assert re.findall(r"\d+", out) == [str(expected)]


def test_main_rejects_an_anchor_without_a_utc_offset(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    args = ["--records", str(RECORDS), "--anchor", "2026-11-03T12:00:00"]

    # Act
    code, _out = _run_main(monkeypatch, capsys, args)

    # Assert
    assert code == 2
    assert _count(session_factory) == 0
