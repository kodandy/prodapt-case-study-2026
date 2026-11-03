"""Tests for seed_dataset, written from docs/specs/sq1.md section 4 (seed) with small hand-built logs."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.seed import MERCHANT_NAMESPACE, SeedResult, merchant_uuid, seed_dataset
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW
from scripts.generate_dataset import (
    CanonicalTransaction,
    Incident,
    PaymentMethod,
    PlantedScenarios,
    RecordKind,
    ScenarioLog,
    Status,
)

WINDOW_END = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
ANCHOR = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
SHIFT = ANCHOR - WINDOW_END
CLOCK_NOW = TEST_NOW + timedelta(minutes=5)


def _clock() -> datetime:
    return CLOCK_NOW


def _tx(
    n: int,
    *,
    merchant_id: str | None = "mch_0001",
    payment_method: PaymentMethod = "card",
    status: Status = "success",
    failure_reason: str | None = None,
    timestamp: datetime = WINDOW_END - timedelta(hours=1),
    record_kind: RecordKind = "merchant_payment",
    card_bin: str | None = "424242",
    acquirer: str | None = "acq_a",
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=uuid.UUID(int=n),
        user_id=uuid.UUID(int=1000 + n),
        merchant_id=merchant_id,
        amount_minor=1000 + n,
        currency="USD",
        payment_method=payment_method,
        status=status,
        failure_reason=failure_reason,
        fraud_score=0.1,
        timestamp=timestamp,
        chargeback_flag=False,
        settlement_status="settled",
        resolution_notes=None,
        kyc_status="verified",
        country_sender="US",
        country_receiver="US",
        record_kind=record_kind,
        scenario="baseline",
        card_bin=card_bin,
        acquirer=acquirer,
        rule_id=None,
        sender_balance_minor=None,
    )


def _incident(
    incident_id: str,
    merchant_id: str | None,
    *,
    resolved_at: datetime | None = datetime(2026, 9, 14, 16, 40, tzinfo=UTC),
    recurrence_within_7d: bool = False,
) -> Incident:
    return Incident(
        incident_id=incident_id,
        title=f"Title of {incident_id}",
        merchant_id=merchant_id,
        started_at=datetime(2026, 9, 14, 13, 0, tzinfo=UTC),
        resolved_at=resolved_at,
        root_cause=f"Root cause of {incident_id}",
        fix_applied=f"Fix of {incident_id}",
        recurrence_within_7d=recurrence_within_7d,
        resolution_notes=f"Notes of {incident_id}",
    )


def _log(transactions: tuple[CanonicalTransaction, ...], incidents: tuple[Incident, ...]) -> ScenarioLog:
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
    return ScenarioLog(transactions=transactions, incidents=incidents, planted=planted)


def _kept_transactions() -> tuple[CanonicalTransaction, ...]:
    return (
        _tx(10, timestamp=WINDOW_END - timedelta(hours=1)),
        _tx(
            11,
            status="failed",
            failure_reason="Issuer unavailable (timeout) for BIN 400000",
            card_bin="400000",
            timestamp=WINDOW_END - timedelta(minutes=30),
        ),
        _tx(
            12,
            status="flagged",
            failure_reason="Flagged by fraud model: velocity anomaly",
            merchant_id="mch_0002",
            timestamp=WINDOW_END - timedelta(minutes=10),
        ),
        _tx(13, status="pending", timestamp=WINDOW_END - timedelta(minutes=5)),
        _tx(14, status="reversed", timestamp=WINDOW_END - timedelta(minutes=2)),
        _tx(15, timestamp=WINDOW_END),
    )


def _ignored_transactions() -> tuple[CanonicalTransaction, ...]:
    return (
        _tx(20, record_kind="p2p_transfer", merchant_id=None, payment_method="bank_transfer", card_bin=None),
        _tx(21, payment_method="wallet", card_bin=None, acquirer=None),
    )


def _future_transactions() -> tuple[CanonicalTransaction, ...]:
    return (
        _tx(30, timestamp=WINDOW_END + timedelta(seconds=1)),
        _tx(31, status="failed", failure_reason="Card expired", timestamp=WINDOW_END + timedelta(hours=2)),
    )


def _incidents() -> tuple[Incident, ...]:
    return (
        _incident("inc_0001", "mch_0001", recurrence_within_7d=True),
        _incident("inc_0002", None, resolved_at=None),
        _incident("inc_0003", "mch_0002"),
    )


def _rows(session_factory: sessionmaker[Session], query: str) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query)).mappings()]


def _counts(session_factory: sessionmaker[Session]) -> dict[str, int]:
    tables = ("payment_facts", "search_documents", "processed_events", "incidents")
    queries = {
        "payment_facts": "SELECT count(*) AS n FROM ops_ai.payment_facts",
        "search_documents": "SELECT count(*) AS n FROM ops_ai.search_documents",
        "processed_events": "SELECT count(*) AS n FROM ops_ai.processed_events",
        "incidents": "SELECT count(*) AS n FROM ops_ai.incidents",
    }
    return {table: int(str(_rows(session_factory, queries[table])[0]["n"])) for table in tables}


def _seed(session_factory: sessionmaker[Session], log: ScenarioLog | None = None) -> SeedResult:
    chosen = log or _log(
        (*_kept_transactions(), *_ignored_transactions(), *_future_transactions()), _incidents()
    )
    return seed_dataset(session_factory, _clock, chosen, ANCHOR)


FACTS_QUERY = (
    "SELECT payment_id, merchant_id, amount_minor, currency, method, state, provider, decline_code, "
    "card_bin, entity_version, occurred_at FROM ops_ai.payment_facts ORDER BY payment_id"
)


# --- payments --------------------------------------------------------------------------------------------


def test_seed_dataset_returns_the_applied_skipped_and_incident_counts(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 6 kept card payments, 2 ignored (p2p, wallet), 2 after the anchor, 3 incidents

    # Act
    result = _seed(session_factory)

    # Assert
    assert result == SeedResult(payments_applied=6, payments_skipped=2, incidents_upserted=3)


def test_seed_dataset_projects_each_kept_payment_into_payment_facts_with_shifted_time(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    expected = [
        {
            "payment_id": tx.transaction_id,
            "merchant_id": merchant_uuid(str(tx.merchant_id)),
            "amount_minor": tx.amount_minor,
            "currency": "USD",
            "method": "card",
            "state": state,
            "provider": "acq_a",
            "decline_code": decline_code,
            "card_bin": tx.card_bin,
            "entity_version": 1,
            "occurred_at": tx.timestamp + SHIFT,
        }
        for tx, state, decline_code in zip(
            _kept_transactions(),
            ["succeeded", "declined", "declined", "unknown", "succeeded", "succeeded"],
            [None, "issuer_unavailable", "fraud_flagged", None, None, None],
            strict=True,
        )
    ]

    # Act
    _seed(session_factory)

    # Assert
    assert _rows(session_factory, FACTS_QUERY) == sorted(expected, key=lambda row: str(row["payment_id"]))


def test_seed_dataset_keeps_a_record_exactly_at_the_anchor_and_skips_one_a_second_after(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    at_anchor = _tx(40, timestamp=WINDOW_END)
    after_anchor = _tx(41, timestamp=WINDOW_END + timedelta(seconds=1))
    log = _log((at_anchor, after_anchor), ())

    # Act
    result = _seed(session_factory, log)

    # Assert
    assert (result.payments_applied, result.payments_skipped) == (1, 1)
    assert [row["payment_id"] for row in _rows(session_factory, FACTS_QUERY)] == [at_anchor.transaction_id]


def test_seed_dataset_never_stores_a_payment_after_the_anchor(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    future_ids = {tx.transaction_id for tx in _future_transactions()}

    # Act
    _seed(session_factory)

    # Assert
    stored = {row["payment_id"] for row in _rows(session_factory, FACTS_QUERY)}
    assert stored & future_ids == set()


def test_seed_dataset_counts_every_record_after_the_anchor_as_skipped_whatever_its_kind(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    future_p2p = _tx(
        50,
        record_kind="p2p_transfer",
        merchant_id=None,
        payment_method="bank_transfer",
        card_bin=None,
        timestamp=WINDOW_END + timedelta(hours=1),
    )
    log = _log((_tx(51), future_p2p), ())

    # Act
    result = _seed(session_factory, log)

    # Assert
    assert result == SeedResult(payments_applied=1, payments_skipped=1, incidents_upserted=0)


def test_seed_dataset_does_not_store_p2p_or_non_card_records(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ignored_ids = {tx.transaction_id for tx in _ignored_transactions()}

    # Act
    _seed(session_factory)

    # Assert
    stored = {row["payment_id"] for row in _rows(session_factory, FACTS_QUERY)}
    assert stored & ignored_ids == set()


def test_seed_dataset_goes_through_the_consumer_projection(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    kept_ids = sorted(str(tx.transaction_id) for tx in _kept_transactions())

    # Act
    _seed(session_factory)

    # Assert: apply_payment_event writes the dedupe row and the payment search document in one go
    processed = _rows(
        session_factory,
        "SELECT consumer, event_id, processed_at FROM ops_ai.processed_events ORDER BY event_id",
    )
    expected_event_ids = sorted(uuid.uuid5(MERCHANT_NAMESPACE, f"seed:{pid}") for pid in kept_ids)
    assert [row["event_id"] for row in processed] == expected_event_ids
    assert {row["consumer"] for row in processed} == {"ops-ai.payment-facts"}
    assert {row["processed_at"] for row in processed} == {CLOCK_NOW}
    documents = _rows(session_factory, "SELECT kind, ref_id FROM ops_ai.search_documents ORDER BY ref_id")
    assert [str(row["ref_id"]) for row in documents] == kept_ids
    assert {row["kind"] for row in documents} == {"payment"}


# --- idempotency -----------------------------------------------------------------------------------------


def test_seed_dataset_second_run_applies_nothing_and_changes_no_row_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory)
    counts_after_first = _counts(session_factory)

    # Act
    second = _seed(session_factory)

    # Assert
    assert second.payments_applied == 0
    assert _counts(session_factory) == counts_after_first
    assert counts_after_first == {
        "payment_facts": 6,
        "search_documents": 6,
        "processed_events": 6,
        "incidents": 3,
    }


def test_seed_dataset_second_run_with_another_anchor_still_applies_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first_log = _log(_kept_transactions(), ())
    seed_dataset(session_factory, _clock, first_log, ANCHOR)
    facts_before = _rows(session_factory, FACTS_QUERY)

    # Act
    second = seed_dataset(session_factory, _clock, first_log, ANCHOR + timedelta(days=1))

    # Assert: dedupe is on event_id, so the history keeps the first anchor
    assert second.payments_applied == 0
    assert _rows(session_factory, FACTS_QUERY) == facts_before


# --- incidents -------------------------------------------------------------------------------------------


def test_seed_dataset_upserts_incidents_with_shifted_times_and_mapped_merchants(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    query = (
        "SELECT incident_id, merchant_id, title, root_cause, fix_applied, resolution_notes, started_at, "
        "resolved_at, recurrence_within_7d FROM ops_ai.incidents ORDER BY incident_id"
    )
    expected = [
        {
            "incident_id": incident.incident_id,
            "merchant_id": None if incident.merchant_id is None else merchant_uuid(incident.merchant_id),
            "title": incident.title,
            "root_cause": incident.root_cause,
            "fix_applied": incident.fix_applied,
            "resolution_notes": incident.resolution_notes,
            "started_at": incident.started_at + SHIFT,
            "resolved_at": None if incident.resolved_at is None else incident.resolved_at + SHIFT,
            "recurrence_within_7d": incident.recurrence_within_7d,
        }
        for incident in _incidents()
    ]

    # Act
    _seed(session_factory)

    # Assert
    assert _rows(session_factory, query) == expected


def test_seed_dataset_stores_a_platform_wide_incident_with_a_null_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    _seed(session_factory)

    # Assert
    rows = _rows(session_factory, "SELECT incident_id FROM ops_ai.incidents WHERE merchant_id IS NULL")
    assert rows == [{"incident_id": "inc_0002"}]


def test_seed_dataset_rerun_updates_a_changed_incident_instead_of_duplicating_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory)
    changed = tuple(
        dataclasses.replace(incident, fix_applied="Updated fix")
        if incident.incident_id == "inc_0001"
        else incident
        for incident in _incidents()
    )
    log = _log((*_kept_transactions(),), changed)

    # Act
    result = seed_dataset(session_factory, _clock, log, ANCHOR)

    # Assert
    assert result.incidents_upserted == 3
    rows = _rows(
        session_factory, "SELECT incident_id, fix_applied FROM ops_ai.incidents ORDER BY incident_id"
    )
    assert rows == [
        {"incident_id": "inc_0001", "fix_applied": "Updated fix"},
        {"incident_id": "inc_0002", "fix_applied": "Fix of inc_0002"},
        {"incident_id": "inc_0003", "fix_applied": "Fix of inc_0003"},
    ]


def test_seed_dataset_with_no_records_and_no_incidents_returns_zero_counts(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log((), ())

    # Act
    result = _seed(session_factory, log)

    # Assert
    assert result == SeedResult(payments_applied=0, payments_skipped=0, incidents_upserted=0)
    assert _counts(session_factory) == {
        "payment_facts": 0,
        "search_documents": 0,
        "processed_events": 0,
        "incidents": 0,
    }


def test_seed_result_exposes_the_three_counts() -> None:
    # Arrange
    result = SeedResult(payments_applied=1, payments_skipped=2, incidents_upserted=3)

    # Act
    fields = dataclasses.asdict(result)

    # Assert
    assert fields == {"payments_applied": 1, "payments_skipped": 2, "incidents_upserted": 3}


def test_seed_result_rejects_assignment_because_it_is_frozen() -> None:
    # Arrange
    result = SeedResult(payments_applied=1, payments_skipped=2, incidents_upserted=3)
    field_name = "payments_applied"

    # Act
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(result, field_name, 5)

    # Assert
    assert result.payments_applied == 1
