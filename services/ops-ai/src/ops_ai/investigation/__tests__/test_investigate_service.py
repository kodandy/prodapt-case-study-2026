"""Tests for investigation/service.py (docs/specs/sq1.md section 7, small hand-built data)."""

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.service import Investigation, ToolCall, investigate
from ops_ai.investigation.tools import BinShare, CodeCount
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=6)
BASELINE_START = WINDOW_START - timedelta(days=7)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
QUESTION = "15% of my card payments are failing this afternoon - what is the root cause and how do we fix it?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INCIDENT_STARTED = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
INCIDENT_RESOLVED = datetime(2026, 9, 14, 16, 40, tzinfo=UTC)
STRONG_TEXT = "issuer issuer issuer issuer BIN 400000 issuer BIN 400000"


@dataclass
class RecordingLlm:
    text: str = NARRATION
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=self.text, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


def _fact(
    session: Session,
    occurred_at: datetime,
    *,
    state: str,
    card_bin: str | None = "411111",
    decline_code: str | None = None,
    merchant_id: uuid.UUID = MERCHANT,
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    session.add(
        PaymentFact(
            payment_id=payment_id,
            merchant_id=merchant_id,
            amount_minor=1500,
            currency="USD",
            method="card",
            state=state,
            provider="acq_a",
            decline_code=decline_code,
            card_bin=card_bin,
            entity_version=1,
            occurred_at=occurred_at,
        )
    )
    return payment_id


def _incident(
    session: Session,
    incident_id: str,
    merchant_id: uuid.UUID | None,
    *,
    title: str = "Issuer outage on BIN 400000",
    fix_applied: str = "Rerouted BIN 400000 to acq_b while the issuer was down",
    resolved: bool = True,
) -> None:
    session.execute(
        text(
            "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
            "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
            "(:incident_id, :merchant_id, :title, 'Issuer timeouts on BIN 400000', :fix_applied, 'notes', "
            ":started_at, :resolved_at, false)"
        ),
        {
            "incident_id": incident_id,
            "merchant_id": merchant_id,
            "title": title,
            "fix_applied": fix_applied,
            "started_at": INCIDENT_STARTED,
            "resolved_at": INCIDENT_RESOLVED if resolved else None,
        },
    )


def _seed_scenario(
    session_factory: sessionmaker[Session],
    *,
    window_failures_by_bin: dict[str, int],
    window_succeeded: int,
    baseline_total: int,
    baseline_failed: int,
    merchant_id: uuid.UUID = MERCHANT,
) -> None:
    """Window records sit one minute apart from the window start; baseline records likewise."""
    with session_factory.begin() as session:
        minute = 1
        for card_bin, count in window_failures_by_bin.items():
            for _ in range(count):
                _fact(
                    session,
                    WINDOW_START + timedelta(minutes=minute),
                    state="declined",
                    card_bin=card_bin,
                    decline_code="issuer_unavailable",
                    merchant_id=merchant_id,
                )
                minute += 1
        for _ in range(window_succeeded):
            _fact(
                session, WINDOW_START + timedelta(minutes=minute), state="succeeded", merchant_id=merchant_id
            )
            minute += 1
        for index in range(baseline_total):
            failed = index < baseline_failed
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if failed else "succeeded",
                decline_code="card_declined" if failed else None,
                merchant_id=merchant_id,
            )


def _run(
    session_factory: sessionmaker[Session],
    llm: RecordingLlm | DownLlm | None = None,
    question: str = QUESTION,
    scope: Scope = SCOPE,
) -> Investigation:
    with session_factory() as session:
        return investigate(session, scope, question, NOW, llm or RecordingLlm())


def _refs(result: Investigation) -> list[str]:
    return [item.ref for item in result.evidence]


# --- intent ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        QUESTION,
        "Why are payments declining?",
        "Card failures yesterday",
        "CARD PAYMENTS FAILING",
        "my customers see declined card transactions",
        "payment failure rate",
    ],
)
def test_investigate_treats_fail_or_declin_with_card_or_payment_as_a_failure_spike(
    session_factory: sessionmaker[Session], question: str
) -> None:
    # Arrange: empty database, only the intent matters

    # Act
    result = _run(session_factory, question=question)

    # Assert
    assert result.intent == "failure_spike"
    assert [call.name for call in result.tool_calls] == [
        "aggregate_failures",
        "compare_baseline",
        "search_incidents",
    ]


@pytest.mark.parametrize(
    "question",
    [
        "What is my settlement balance?",
        "Show my card",
        "Why is the app failing?",
        "I need a payment receipt",
        "Hello",
    ],
)
def test_investigate_marks_any_other_question_as_unsupported_without_tools_or_llm(
    session_factory: sessionmaker[Session], question: str
) -> None:
    # Arrange
    llm = RecordingLlm()

    # Act
    result = _run(session_factory, llm=llm, question=question)

    # Assert
    assert result == Investigation(
        intent="unsupported",
        cause_label="unsupported",
        window=None,
        baseline=None,
        suggested_fixes=(),
        tool_calls=(),
        evidence=(),
        mode="t2_fallback",
        text=None,
        model=None,
    )
    assert llm.calls == []


# --- tool calls ------------------------------------------------------------------------------------------


def test_investigate_records_the_three_tool_calls_in_order_with_iso_arguments_and_the_top_bin_query(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory,
        window_failures_by_bin={"400000": 12},
        window_succeeded=88,
        baseline_total=100,
        baseline_failed=2,
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert result.tool_calls == (
        ToolCall(
            name="aggregate_failures",
            args={"start": WINDOW_START.isoformat(), "end": NOW.isoformat()},
        ),
        ToolCall(name="compare_baseline", args={"window_start": WINDOW_START.isoformat(), "days": "7"}),
        ToolCall(name="search_incidents", args={"query": "BIN 400000 issuer"}),
    )


def test_investigate_searches_card_failures_when_the_window_has_no_failed_bin(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory, window_failures_by_bin={}, window_succeeded=20, baseline_total=20, baseline_failed=0
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert result.tool_calls[2] == ToolCall(name="search_incidents", args={"query": "card failures"})
    assert result.window is not None
    assert result.window.by_bin == ()


def test_investigate_window_is_the_six_hours_before_now_and_baseline_the_seven_days_before_the_window(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory,
        window_failures_by_bin={"400000": 12, "424242": 2},
        window_succeeded=66,
        baseline_total=200,
        baseline_failed=4,
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert result.window is not None
    assert result.baseline is not None
    assert (result.window.start, result.window.end) == (WINDOW_START, NOW)
    assert (result.window.total, result.window.failed) == (80, 14)
    assert result.window.by_bin == (BinShare("400000", 12, 12), BinShare("424242", 2, 2))
    assert result.window.by_code == (CodeCount("issuer_unavailable", 14),)
    assert (result.baseline.start, result.baseline.end) == (BASELINE_START, WINDOW_START)
    assert (result.baseline.total, result.baseline.failed) == (200, 4)
    assert result.baseline.rate == 4 / 200


# --- cause label -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures_by_bin", "succeeded", "baseline_total", "baseline_failed", "expected"),
    [
        ({"400000": 12}, 88, 100, 2, "issuer_outage_on_bin"),
        ({"400000": 10}, 90, 100, 2, "issuer_outage_on_bin"),
        ({"400000": 9}, 11, 100, 0, "no_anomaly"),
        ({"400000": 10}, 90, 100, 5, "issuer_outage_on_bin"),
        ({"400000": 10}, 90, 100, 6, "no_anomaly"),
        ({"400000": 5, "411111": 5}, 90, 100, 2, "issuer_outage_on_bin"),
        ({"400000": 4, "411111": 3, "424242": 3}, 90, 100, 2, "elevated_failures"),
        ({"400000": 10}, 40, 0, 0, "issuer_outage_on_bin"),
        ({"400000": 10}, 90, 100, 10, "no_anomaly"),
        ({}, 50, 100, 2, "no_anomaly"),
    ],
    ids=[
        "twelve_failures_all_on_one_bin",
        "exactly_ten_failures",
        "nine_failures_is_below_the_floor",
        "rate_exactly_twice_the_baseline",
        "rate_just_under_twice_the_baseline",
        "top_bin_holds_exactly_half",
        "top_bin_holds_forty_percent",
        "baseline_without_records",
        "rate_equal_to_the_baseline",
        "no_failures_at_all",
    ],
)
def test_investigate_cause_label_follows_the_rate_floor_and_top_bin_share_thresholds(
    session_factory: sessionmaker[Session],
    failures_by_bin: dict[str, int],
    succeeded: int,
    baseline_total: int,
    baseline_failed: int,
    expected: str,
) -> None:
    # Arrange
    _seed_scenario(
        session_factory,
        window_failures_by_bin=failures_by_bin,
        window_succeeded=succeeded,
        baseline_total=baseline_total,
        baseline_failed=baseline_failed,
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert result.cause_label == expected


def test_investigate_cause_label_never_comes_from_the_llm_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory, window_failures_by_bin={}, window_succeeded=30, baseline_total=30, baseline_failed=0
    )
    llm = RecordingLlm(text="The root cause is issuer_outage_on_bin with a 99.9% failure rate.")

    # Act
    result = _run(session_factory, llm=llm)

    # Assert
    assert result.cause_label == "no_anomaly"
    assert result.text == "The root cause is issuer_outage_on_bin with a 99.9% failure rate."
    assert result.window is not None
    assert (result.window.total, result.window.failed) == (30, 0)


# --- suggested fixes -------------------------------------------------------------------------------------


def test_investigate_suggested_fixes_are_the_incident_fixes_in_hit_order_without_duplicates(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory,
        window_failures_by_bin={"400000": 12},
        window_succeeded=88,
        baseline_total=100,
        baseline_failed=2,
    )
    with session_factory.begin() as session:
        _incident(session, "inc_resolved_strong", MERCHANT, title=STRONG_TEXT, fix_applied="Fix A")
        _incident(session, "inc_resolved_weak", None, fix_applied="Fix A")
        _incident(
            session,
            "inc_unresolved",
            MERCHANT,
            title=STRONG_TEXT + " issuer",
            fix_applied="Fix U",
            resolved=False,
        )

    # Act
    result = _run(session_factory)

    # Assert
    assert result.suggested_fixes == ("Fix A", "Fix U")


def test_investigate_suggested_fixes_are_empty_for_no_anomaly_even_when_incidents_match(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory,
        window_failures_by_bin={"400000": 9},
        window_succeeded=11,
        baseline_total=100,
        baseline_failed=0,
    )
    with session_factory.begin() as session:
        _incident(session, "inc_a", MERCHANT)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.cause_label == "no_anomaly"
    assert result.suggested_fixes == ()


# --- evidence --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceScenario:
    newest_first_failed_ids: tuple[uuid.UUID, ...]
    """The failed payments on BIN 400000 inside the window, newest first (10 of them)."""
    excluded_ids: frozenset[uuid.UUID]


def _seed_evidence_scenario(session_factory: sessionmaker[Session]) -> EvidenceScenario:
    """Window of 80 card payments, 12 failed (10 on 400000, 2 on 424242); baseline 200 with 4 failed."""
    on_bin: list[uuid.UUID] = []
    excluded: set[uuid.UUID] = set()
    with session_factory.begin() as session:
        for step in range(1, 11):
            on_bin.append(
                _fact(
                    session,
                    WINDOW_START + timedelta(minutes=step * 10),
                    state="declined",
                    card_bin="400000",
                    decline_code="issuer_unavailable",
                )
            )
        for minute in (105, 110):
            excluded.add(
                _fact(
                    session,
                    WINDOW_START + timedelta(minutes=minute),
                    state="declined",
                    card_bin="424242",
                    decline_code="card_declined",
                )
            )
        excluded.add(
            _fact(session, WINDOW_START + timedelta(minutes=120), state="succeeded", card_bin="400000")
        )
        for minute in range(121, 188):
            _fact(session, WINDOW_START + timedelta(minutes=minute), state="succeeded", card_bin="411111")
        excluded.add(
            _fact(session, NOW, state="declined", card_bin="400000", decline_code="issuer_unavailable")
        )
        excluded.add(
            _fact(
                session,
                WINDOW_START + timedelta(minutes=150),
                state="declined",
                card_bin="400000",
                decline_code="issuer_unavailable",
                merchant_id=OTHER_MERCHANT,
            )
        )
        for index in range(200):
            failed = index < 4
            _fact(
                session,
                BASELINE_START + timedelta(minutes=index + 1),
                state="declined" if failed else "succeeded",
                decline_code="card_declined" if failed else None,
            )
        _incident(session, "inc_a_strong", MERCHANT, title=STRONG_TEXT, fix_applied="Fix A")
        _incident(session, "inc_b_weak", None, fix_applied="Fix A")
        _incident(
            session,
            "inc_c_unresolved",
            MERCHANT,
            title=STRONG_TEXT + " issuer",
            fix_applied="Fix C",
            resolved=False,
        )
    return EvidenceScenario(newest_first_failed_ids=tuple(reversed(on_bin)), excluded_ids=frozenset(excluded))


def test_investigate_evidence_refs_come_in_the_specified_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scenario = _seed_evidence_scenario(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert _refs(result) == [
        "aggregate:window",
        "aggregate:baseline",
        "aggregate:bin:400000",
        "incident:inc_a_strong",
        "incident:inc_b_weak",
        "incident:inc_c_unresolved",
        *(f"payment:{pid}" for pid in scenario.newest_first_failed_ids[:5]),
    ]


def test_investigate_window_and_baseline_evidence_carry_total_failed_and_a_one_decimal_percentage(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_evidence_scenario(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    summaries = {item.ref: item.summary for item in result.evidence}
    assert [token in summaries["aggregate:window"] for token in ("80", "12", "15.0%")] == [True, True, True]
    assert [token in summaries["aggregate:baseline"] for token in ("200", "4", "2.0%")] == [True, True, True]
    assert result.window is not None
    assert (result.window.total, result.window.failed) == (80, 12)


def test_investigate_bin_evidence_carries_the_failed_count_and_the_share_of_window_failures(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_evidence_scenario(session_factory)

    # Act
    result = _run(session_factory)

    # Assert: 10 of the 12 window failures are on 400000, that is 83.3 percent
    summary = next(item.summary for item in result.evidence if item.ref == "aggregate:bin:400000")
    assert "10" in summary
    assert any(share in summary for share in ("83.3%", "83%"))


def test_investigate_incident_evidence_carries_the_title_and_the_fix(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_evidence_scenario(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    summary = next(item.summary for item in result.evidence if item.ref == "incident:inc_a_strong")
    assert STRONG_TEXT in summary
    assert "Fix A" in summary


def test_investigate_payment_evidence_is_the_five_newest_failed_payments_on_the_top_bin_in_the_window(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scenario = _seed_evidence_scenario(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    payment_refs = [ref for ref in _refs(result) if ref.startswith("payment:")]
    assert payment_refs == [f"payment:{pid}" for pid in scenario.newest_first_failed_ids[:5]]
    assert [
        ref for ref in payment_refs if uuid.UUID(ref.removeprefix("payment:")) in scenario.excluded_ids
    ] == []


def test_investigate_without_any_failure_has_only_the_two_aggregate_evidence_items(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory, window_failures_by_bin={}, window_succeeded=20, baseline_total=20, baseline_failed=0
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert _refs(result) == ["aggregate:window", "aggregate:baseline"]
    assert result.suggested_fixes == ()


# --- LLM narration ---------------------------------------------------------------------------------------


def test_investigate_asks_the_llm_once_with_the_question_and_the_evidence_and_returns_its_narration(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_evidence_scenario(session_factory)
    llm = RecordingLlm()

    # Act
    result = _run(session_factory, llm=llm)

    # Assert
    assert llm.calls == [LlmRequest(question=QUESTION, evidence=result.evidence)]
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)
    assert all(isinstance(item, EvidenceItem) for item in result.evidence)
    assert result.cause_label == "issuer_outage_on_bin"


def test_investigate_with_an_unavailable_llm_returns_t2_fallback_with_the_same_other_fields(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_evidence_scenario(session_factory)
    with_llm = _run(session_factory, llm=RecordingLlm())
    down = DownLlm()

    # Act
    fallback = _run(session_factory, llm=down)

    # Assert
    assert len(down.calls) == 1
    assert fallback == dataclasses.replace(with_llm, mode="t2_fallback", text=None, model=None)


# --- scope -----------------------------------------------------------------------------------------------


def test_investigate_never_uses_another_merchants_payments_or_incidents(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_scenario(
        session_factory,
        window_failures_by_bin={"400000": 30},
        window_succeeded=70,
        baseline_total=100,
        baseline_failed=1,
        merchant_id=OTHER_MERCHANT,
    )
    with session_factory.begin() as session:
        _incident(session, "inc_other", OTHER_MERCHANT)
    llm = RecordingLlm()

    # Act
    result = _run(session_factory, llm=llm)

    # Assert
    assert result.cause_label == "no_anomaly"
    assert result.window is not None
    assert (result.window.total, result.window.failed, result.window.by_bin) == (0, 0, ())
    assert result.suggested_fixes == ()
    assert _refs(result) == ["aggregate:window", "aggregate:baseline"]
    assert [item.ref for call in llm.calls for item in call.evidence] == [
        "aggregate:window",
        "aggregate:baseline",
    ]
