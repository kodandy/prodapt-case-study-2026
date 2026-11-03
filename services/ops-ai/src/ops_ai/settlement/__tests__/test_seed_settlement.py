"""Tests for seed_settlement and python -m ops_ai.settlement, from docs/specs/sq4.md section 2."""

import re
import runpy
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.seed import merchant_uuid, seed_shift
from ops_ai.settlement.seed import seed_settlement
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
SHIFT = ANCHOR - WINDOW_END
RECORDS = 1000
UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

ROWS_QUERY = (
    "SELECT payment_id, merchant_id, amount_minor, currency, captured_at, statement_period, refunded, "
    "chargeback, ingest_source FROM ops_ai.settlement_entries ORDER BY payment_id"
)


def _tx(
    n: int,
    *,
    record_kind: RecordKind = "merchant_payment",
    status: Status = "success",
    payment_method: PaymentMethod = "card",
    merchant_id: str | None = "mch_0003",
    chargeback_flag: bool = False,
    amount_minor: int = 12_345,
    currency: str = "USD",
    timestamp: datetime = WINDOW_END - timedelta(hours=1),
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=uuid.UUID(int=n),
        user_id=uuid.UUID(int=1000 + n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        payment_method=payment_method,
        status=status,
        failure_reason=None,
        fraud_score=0.1,
        timestamp=timestamp,
        chargeback_flag=chargeback_flag,
        settlement_status="settled",
        resolution_notes=None,
        kyc_status="verified",
        country_sender="US",
        country_receiver="US",
        record_kind=record_kind,
        scenario="baseline",
        card_bin="424242",
        acquirer="acq_a",
        rule_id=None,
        sender_balance_minor=None,
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
    session_factory: sessionmaker[Session], query: str = ROWS_QUERY, **params: object
) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session]) -> int:
    return len(_rows(session_factory, "SELECT payment_id FROM ops_ai.settlement_entries"))


def _run_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], args: list[str]
) -> tuple[int | str | None, str]:
    monkeypatch.setattr(sys, "argv", ["python -m ops_ai.settlement", *args])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("ops_ai.settlement", run_name="__main__", alter_sys=True)
    return exc.value.code, capsys.readouterr().out


def _expected_count(seed: int, records: int) -> int:
    log = generate(seed=seed, records=records)
    shift = seed_shift(log.planted, ANCHOR)
    return sum(
        1
        for tx in log.transactions
        if tx.record_kind == "merchant_payment"
        and tx.payment_method == "card"
        and tx.merchant_id is not None
        and tx.status in ("success", "reversed")
        and tx.timestamp + shift <= ANCHOR
    )


# --- seed_settlement: what is stored ---------------------------------------------------------------------


def test_seed_settlement_stores_one_entry_per_captured_card_payment_with_the_specified_fields(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    plain = _tx(10, amount_minor=12_345)
    refunded = _tx(11, status="reversed", amount_minor=2_000, currency="EUR")
    charged_back = _tx(12, chargeback_flag=True, amount_minor=500_000, currency="JPY")
    log = _log((plain, refunded, charged_back))

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    merchant = merchant_uuid("mch_0003")
    assert inserted == 3
    assert _rows(session_factory) == [
        {
            "payment_id": plain.transaction_id,
            "merchant_id": merchant,
            "amount_minor": 12_345,
            "currency": "USD",
            "captured_at": plain.timestamp + SHIFT,
            "statement_period": "2026-10",
            "refunded": False,
            "chargeback": False,
            "ingest_source": "seed",
        },
        {
            "payment_id": refunded.transaction_id,
            "merchant_id": merchant,
            "amount_minor": 2_000,
            "currency": "EUR",
            "captured_at": refunded.timestamp + SHIFT,
            "statement_period": "2026-10",
            "refunded": True,
            "chargeback": False,
            "ingest_source": "seed",
        },
        {
            "payment_id": charged_back.transaction_id,
            "merchant_id": merchant,
            "amount_minor": 500_000,
            "currency": "JPY",
            "captured_at": charged_back.timestamp + SHIFT,
            "statement_period": "2026-10",
            "refunded": False,
            "chargeback": True,
            "ingest_source": "seed",
        },
    ]


def test_seed_settlement_keeps_both_flags_for_a_reversed_payment_that_was_also_charged_back(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    both = _tx(10, status="reversed", chargeback_flag=True)

    # Act
    seed_settlement(session_factory, _log((both,)), ANCHOR)

    # Assert
    rows = _rows(session_factory)
    assert [(row["refunded"], row["chargeback"]) for row in rows] == [(True, True)]


def test_seed_settlement_uses_the_merchant_uuid_of_the_external_merchant_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _tx(10, merchant_id="mch_0003")
    second = _tx(11, merchant_id="mch_0017")

    # Act
    seed_settlement(session_factory, _log((first, second)), ANCHOR)

    # Assert
    assert {row["payment_id"]: row["merchant_id"] for row in _rows(session_factory)} == {
        first.transaction_id: merchant_uuid("mch_0003"),
        second.transaction_id: merchant_uuid("mch_0017"),
    }


# --- seed_settlement: the period label and the shift -----------------------------------------------------


def test_seed_settlement_uses_the_same_shift_as_the_payment_seed_for_captured_at(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    tx = _tx(10, timestamp=WINDOW_END - timedelta(minutes=30))
    log = _log((tx,))
    shift = seed_shift(log.planted, ANCHOR)

    # Act
    seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert _rows(session_factory)[0]["captured_at"] == tx.timestamp + shift == ANCHOR - timedelta(minutes=30)


def test_seed_settlement_labels_the_period_with_the_unshifted_month(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: shifted by 2 days 18 hours, this one lands in November, but the acquirer labelled it October
    late_october = _tx(10, timestamp=datetime(2026, 10, 31, 17, 0, tzinfo=UTC))

    # Act
    seed_settlement(session_factory, _log((late_october,)), ANCHOR)

    # Assert
    row = _rows(session_factory)[0]
    assert row["captured_at"] == datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
    assert row["statement_period"] == "2026-10"


def test_seed_settlement_labels_the_last_second_of_september_and_the_first_of_october_apart(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: both shift into October, the label stays the unshifted month
    september = _tx(10, timestamp=datetime(2026, 9, 30, 23, 59, 59, tzinfo=UTC))
    october = _tx(11, timestamp=datetime(2026, 10, 1, 0, 0, 0, tzinfo=UTC))

    # Act
    seed_settlement(session_factory, _log((september, october)), ANCHOR)

    # Assert
    assert {row["payment_id"]: row["statement_period"] for row in _rows(session_factory)} == {
        september.transaction_id: "2026-09",
        october.transaction_id: "2026-10",
    }


# --- seed_settlement: what is skipped --------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "flagged", "pending"])
def test_seed_settlement_skips_a_card_payment_that_was_not_captured(
    session_factory: sessionmaker[Session], status: Status
) -> None:
    # Arrange
    kept = _tx(10)
    skipped = _tx(11, status=status)

    # Act
    inserted = seed_settlement(session_factory, _log((kept, skipped)), ANCHOR)

    # Assert
    assert inserted == 1
    assert [row["payment_id"] for row in _rows(session_factory)] == [kept.transaction_id]


@pytest.mark.parametrize("method", ["wallet", "upi", "bank_transfer"])
def test_seed_settlement_skips_a_merchant_payment_that_is_not_a_card_payment(
    session_factory: sessionmaker[Session], method: PaymentMethod
) -> None:
    # Arrange
    kept = _tx(10)
    skipped = _tx(11, payment_method=method)

    # Act
    inserted = seed_settlement(session_factory, _log((kept, skipped)), ANCHOR)

    # Assert
    assert inserted == 1
    assert [row["payment_id"] for row in _rows(session_factory)] == [kept.transaction_id]


def test_seed_settlement_skips_p2p_transfers_and_payments_without_a_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    kept = _tx(10)
    transfer = _tx(11, record_kind="p2p_transfer", payment_method="bank_transfer", merchant_id=None)
    transfer_with_card = _tx(12, record_kind="p2p_transfer", merchant_id=None)
    no_merchant = _tx(13, merchant_id=None)

    # Act
    inserted = seed_settlement(
        session_factory, _log((kept, transfer, transfer_with_card, no_merchant)), ANCHOR
    )

    # Assert
    assert inserted == 1
    assert [row["payment_id"] for row in _rows(session_factory)] == [kept.transaction_id]


def test_seed_settlement_keeps_a_payment_exactly_at_the_anchor_and_skips_one_second_after(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    at_anchor = _tx(10, timestamp=WINDOW_END)
    before = _tx(11, timestamp=WINDOW_END - timedelta(seconds=1))
    after = _tx(12, timestamp=WINDOW_END + timedelta(seconds=1))
    much_later = _tx(13, timestamp=WINDOW_END + timedelta(hours=2))

    # Act
    inserted = seed_settlement(session_factory, _log((at_anchor, before, after, much_later)), ANCHOR)

    # Assert
    assert inserted == 2
    assert {row["payment_id"] for row in _rows(session_factory)} == {
        at_anchor.transaction_id,
        before.transaction_id,
    }


def test_seed_settlement_with_nothing_to_load_returns_zero_and_writes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((_tx(10, status="failed"), _tx(11, payment_method="wallet")))

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 0
    assert _count(session_factory) == 0


# --- seed_settlement: idempotence ------------------------------------------------------------------------


def test_seed_settlement_second_run_returns_zero_and_leaves_the_rows_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((_tx(10), _tx(11, status="reversed")))
    seed_settlement(session_factory, log, ANCHOR)
    after_first = _rows(session_factory)

    # Act
    second = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert second == 0
    assert _rows(session_factory) == after_first


def test_seed_settlement_a_re_run_with_a_later_anchor_keeps_the_first_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((_tx(10),))
    seed_settlement(session_factory, log, ANCHOR)
    after_first = _rows(session_factory)

    # Act
    second = seed_settlement(session_factory, log, ANCHOR + timedelta(days=1))

    # Assert
    assert second == 0
    assert _rows(session_factory) == after_first


def test_seed_settlement_a_re_run_inserts_only_the_new_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _tx(10)
    seed_settlement(session_factory, _log((first,)), ANCHOR)
    second = _tx(11)

    # Act
    inserted = seed_settlement(session_factory, _log((first, second)), ANCHOR)

    # Assert
    assert inserted == 1
    assert [row["payment_id"] for row in _rows(session_factory)] == [
        first.transaction_id,
        second.transaction_id,
    ]


def test_seed_settlement_inserts_more_rows_than_one_batch(session_factory: sessionmaker[Session]) -> None:
    # Arrange: the transfer seed batches 500 rows per statement
    log = _log(tuple(_tx(10 + n, amount_minor=1_000 + n) for n in range(1_203)))

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 1_203
    assert _count(session_factory) == 1_203


# --- seed_settlement over the generated dataset ----------------------------------------------------------


def test_seed_settlement_count_equals_the_captured_card_records_of_the_log_at_or_before_the_anchor(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    expected = _expected_count(42, RECORDS)

    # Act
    inserted = seed_settlement(session_factory, generate(seed=42, records=RECORDS), ANCHOR)

    # Assert
    assert expected > 60
    assert inserted == expected
    assert _count(session_factory) == expected
    assert {row["ingest_source"] for row in _rows(session_factory)} == {"seed"}


def test_seed_settlement_stores_the_planted_sq4_merchant_records_with_their_flags(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = generate(seed=42, records=RECORDS)
    shift = seed_shift(log.planted, ANCHOR)
    planted = [
        tx for tx in log.transactions if tx.scenario == "sq4_settlement" and tx.timestamp + shift <= ANCHOR
    ]

    # Act
    seed_settlement(session_factory, log, ANCHOR)

    # Assert
    stored = {
        row["payment_id"]: row
        for row in _rows(
            session_factory,
            "SELECT payment_id, merchant_id, amount_minor, currency, statement_period, refunded, chargeback "
            "FROM ops_ai.settlement_entries WHERE merchant_id = :merchant_id",
            merchant_id=merchant_uuid(log.planted.sq4_merchant_id),
        )
    }
    assert len(planted) == 60
    for tx in planted:
        assert stored[tx.transaction_id] == {
            "payment_id": tx.transaction_id,
            "merchant_id": merchant_uuid("mch_0003"),
            "amount_minor": tx.amount_minor,
            "currency": tx.currency,
            "statement_period": "2026-10",
            "refunded": tx.status == "reversed",
            "chargeback": tx.chargeback_flag,
        }


# --- python -m ops_ai.settlement -------------------------------------------------------------------------


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


def test_main_prints_no_merchant_user_or_payment_ids(
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Arrange
    log = generate(seed=42, records=RECORDS)
    merchants = {str(merchant_uuid(tx.merchant_id)) for tx in log.transactions if tx.merchant_id is not None}
    external_merchants = {tx.merchant_id for tx in log.transactions if tx.merchant_id is not None}

    # Act
    _code, out = _run_main(
        monkeypatch, capsys, ["--seed", "42", "--records", str(RECORDS), "--anchor", ANCHOR_ISO]
    )

    # Assert
    assert [m for m in merchants if m in out] == []
    assert [m for m in external_merchants if m in out] == []
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
