"""Golden test for investigate over the generated dataset (docs/specs/sq1.md section 7, Golden)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import PaymentEventV1
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.db import make_session_factory
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.investigation.service import Investigation, investigate
from ops_ai.scope import Scope
from ops_ai.seed import SeedResult, merchant_uuid, seed_dataset
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import DATA_SCHEMAS
from scripts.generate_dataset import ScenarioLog, generate, to_export_row

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
QUESTION = "15% of my card payments are failing this afternoon - what is the root cause and how do we fix it?"
SQ1_MERCHANT = "mch_0001"
OTHER_MERCHANT = "mch_0002"
TOOL_NAMES = ["aggregate_failures", "compare_baseline", "search_incidents"]


@dataclass(frozen=True)
class Golden:
    session_factory: sessionmaker[Session]
    log: ScenarioLog
    result: SeedResult


@pytest.fixture(scope="module")
def golden(engine: Engine) -> Golden:
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
    log = generate(seed=42)
    result = seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR)
    return Golden(session_factory=session_factory, log=log, result=result)


def _investigate(golden: Golden, merchant: str = SQ1_MERCHANT) -> Investigation:
    with golden.session_factory() as session:
        return investigate(session, Scope(merchant_id=merchant_uuid(merchant)), QUESTION, ANCHOR, MockLlm())


def _window_rows(golden: Golden) -> list[dict[str, str | float | int | bool | None]]:
    planted = golden.log.planted
    rows = [to_export_row(tx) for tx in golden.log.transactions]
    return [
        row
        for row in rows
        if row["merchant_id"] == planted.sq1_merchant_id
        and row["payment_method"] == "card"
        and planted.sq1_window_start <= datetime.fromisoformat(str(row["timestamp"])) < planted.sq1_window_end
    ]


def test_golden_window_total_and_failed_equal_the_counts_from_the_export_rows(golden: Golden) -> None:
    # Arrange
    rows = _window_rows(golden)
    expected_total = len(rows)
    expected_failed = sum(1 for row in rows if row["status"] in ("failed", "flagged"))

    # Act
    result = _investigate(golden)

    # Assert
    assert result.window is not None
    assert (result.window.total, result.window.failed) == (expected_total, expected_failed)
    assert expected_total >= 200


def test_golden_window_covers_the_six_hours_ending_at_now(golden: Golden) -> None:
    # Arrange / Act
    result = _investigate(golden)

    # Assert
    assert result.window is not None
    assert (result.window.start, result.window.end) == (ANCHOR - timedelta(hours=6), ANCHOR)


def test_golden_window_rate_is_between_12_and_18_percent_and_baseline_rate_below_5_percent(
    golden: Golden,
) -> None:
    # Arrange / Act
    result = _investigate(golden)

    # Assert
    assert result.window is not None
    assert result.baseline is not None
    assert 0.12 <= result.window.rate <= 0.18
    assert result.baseline.rate < 0.05


def test_golden_cause_is_an_issuer_outage_on_bin_400000(golden: Golden) -> None:
    # Arrange
    on_bin_failures = sum(
        1 for row in _window_rows(golden) if row["status"] == "failed" and row["card_bin"] == "400000"
    )

    # Act
    result = _investigate(golden)

    # Assert
    assert result.intent == "failure_spike"
    assert result.cause_label == "issuer_outage_on_bin"
    assert result.window is not None
    assert result.window.by_bin[0].card_bin == "400000"
    assert result.window.by_bin[0].failed == on_bin_failures


def test_golden_evidence_cites_the_historical_incident_and_the_fix_names_acq_b(golden: Golden) -> None:
    # Arrange / Act
    result = _investigate(golden)

    # Assert
    refs = [item.ref for item in result.evidence]
    assert "incident:inc_0001" in refs
    assert "acq_b" in result.suggested_fixes[0]


def test_golden_tool_calls_are_the_three_tools_in_order(golden: Golden) -> None:
    # Arrange / Act
    result = _investigate(golden)

    # Assert
    assert [call.name for call in result.tool_calls] == TOOL_NAMES
    assert result.tool_calls[2].args == {"query": "BIN 400000 issuer"}


def test_golden_the_mock_llm_narrates_the_evidence(golden: Golden) -> None:
    # Arrange / Act
    result = _investigate(golden)

    # Assert
    assert (result.mode, result.model) == ("llm", "mock")
    assert result.text is not None
    assert "incident:inc_0001" in result.text


def test_golden_another_merchant_sees_no_anomaly_and_no_sq1_incident(golden: Golden) -> None:
    # Arrange / Act
    result = _investigate(golden, OTHER_MERCHANT)

    # Assert
    refs = [item.ref for item in result.evidence]
    assert result.cause_label == "no_anomaly"
    assert "incident:inc_0001" not in refs
    assert result.suggested_fixes == ()


def test_golden_live_declined_charge_adds_exactly_one_failure_and_one_payment_to_the_window(
    golden: Golden,
) -> None:
    # Arrange
    before = _investigate(golden)
    payment_id = uuid.uuid4()
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=ANCHOR - timedelta(minutes=1),
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant_uuid(SQ1_MERCHANT),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="issuer_unavailable",
        card_bin="400000",
        card_last4="0119",
    )

    # Act
    try:
        with golden.session_factory.begin() as session:
            apply_payment_event(session, lambda: ANCHOR, event)
        after = _investigate(golden)
    finally:
        with golden.session_factory.begin() as session:
            session.execute(
                text("DELETE FROM ops_ai.payment_facts WHERE payment_id = :id"), {"id": payment_id}
            )
            session.execute(
                text("DELETE FROM ops_ai.search_documents WHERE ref_id = :id"), {"id": payment_id}
            )
            session.execute(
                text("DELETE FROM ops_ai.processed_events WHERE event_id = :id"), {"id": event.event_id}
            )

    # Assert
    assert before.window is not None
    assert after.window is not None
    assert after.window.failed == before.window.failed + 1
    assert after.window.total == before.window.total + 1
    payment_refs = [item.ref for item in after.evidence if item.ref.startswith("payment:")]
    assert payment_refs[0] == f"payment:{payment_id}"
    assert after.cause_label == "issuer_outage_on_bin"
