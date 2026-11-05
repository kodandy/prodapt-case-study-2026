"""Tests for investigation/failures.py, written from docs/specs/failure-tools.md section 8."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.grounding import ABSTAIN_TEXT, Claim
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError, MockLlm
from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.failures import FailureAnswer, investigate_failures
from ops_ai.investigation.playbook import Playbook, load_playbook
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=6)
BASELINE_START = WINDOW_START - timedelta(days=7)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
QUESTION = "Why are card payments failing this afternoon?"
LLM_MODEL = "recording-model-1"
INCIDENT_TEXT_TITLE = "Issuer unavailable on BIN 400000"
INCIDENT_ROOT_CAUSE = "Issuer timeouts on BIN 400000"
INCIDENT_FIX = "Rerouted BIN 400000 to acq_b while the issuer was down"
INCIDENT_STARTED = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
INCIDENT_RESOLVED = datetime(2026, 9, 14, 16, 40, tzinfo=UTC)


@dataclass
class RecordingLlm:
    """Records every request and narrates with MockLlm (which cites every evidence ref)."""

    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return MockLlm().complete(request)


@dataclass
class FixedTextLlm:
    narration: str
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=self.narration, model=LLM_MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


@dataclass
class SessionProbeLlm:
    """Notes whether the caller's session still holds a transaction when the LLM is called."""

    session: Session | None = None
    in_transaction: list[bool] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        assert self.session is not None
        self.in_transaction.append(self.session.in_transaction())
        return MockLlm().complete(request)


@dataclass
class FakeToolLlm:
    plan: ToolPlan
    requests: list[ToolChoiceRequest] = field(default_factory=list)

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        self.requests.append(request)
        return self.plan


def _fact(
    session: Session,
    occurred_at: datetime,
    *,
    state: str,
    card_bin: str,
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


def _incident(session: Session, incident_id: str, merchant_id: uuid.UUID | None) -> None:
    session.execute(
        text(
            "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
            "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
            "(:incident_id, :merchant_id, :title, :root_cause, :fix_applied, 'notes', :started_at, "
            ":resolved_at, false)"
        ),
        {
            "incident_id": incident_id,
            "merchant_id": merchant_id,
            "title": INCIDENT_TEXT_TITLE,
            "root_cause": INCIDENT_ROOT_CAUSE,
            "fix_applied": INCIDENT_FIX,
            "started_at": INCIDENT_STARTED,
            "resolved_at": INCIDENT_RESOLVED,
        },
    )


def _seed_baseline(
    session: Session, *, total: int, failed_every: int | None, merchant_id: uuid.UUID = MERCHANT
) -> None:
    """`total` card payments on BIN 411111, one every 40 minutes; every `failed_every`th one is declined."""
    for index in range(total):
        failed = failed_every is not None and index % failed_every == 0
        _fact(
            session,
            BASELINE_START + timedelta(minutes=1 + 40 * index),
            state="declined" if failed else "succeeded",
            card_bin="411111",
            decline_code="card_declined" if failed else None,
            merchant_id=merchant_id,
        )


def _seed_sq1(session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT) -> list[uuid.UUID]:
    """The SQ1 spike. Returns the ten issuer_unavailable failure ids, oldest first.

    Window (06:00-12:00): ten failures on BIN 400000 (issuer_unavailable) from 10:30 to 10:39, two
    card_declined failures on BIN 411111 at 07:00 and 07:30, eight successes. Baseline: 200 payments
    with 4 failures (2 %).
    """
    issuer_failures: list[uuid.UUID] = []
    with session_factory.begin() as session:
        _seed_baseline(session, total=200, failed_every=50, merchant_id=merchant_id)
        for minute in range(10):
            issuer_failures.append(
                _fact(
                    session,
                    NOW - timedelta(hours=1, minutes=30 - minute),
                    state="declined",
                    card_bin="400000",
                    decline_code="issuer_unavailable",
                    merchant_id=merchant_id,
                )
            )
        for at in (WINDOW_START + timedelta(hours=1), WINDOW_START + timedelta(hours=1, minutes=30)):
            _fact(
                session,
                at,
                state="declined",
                card_bin="411111",
                decline_code="card_declined",
                merchant_id=merchant_id,
            )
        for index in range(8):
            _fact(
                session,
                WINDOW_START + timedelta(minutes=5 + 45 * index),
                state="succeeded",
                card_bin="411111",
                merchant_id=merchant_id,
            )
        _incident(session, "inc_sq1" if merchant_id == MERCHANT else "inc_other", merchant_id)
    return issuer_failures


def _run(
    session_factory: sessionmaker[Session],
    llm: RecordingLlm | FixedTextLlm | DownLlm | SessionProbeLlm | None = None,
    *,
    question: str = QUESTION,
    scope: Scope = SCOPE,
    tool_llm: FakeToolLlm | None = None,
    playbook: Playbook | None = None,
) -> FailureAnswer:
    with session_factory() as session:
        if isinstance(llm, SessionProbeLlm):
            llm.session = session
        return investigate_failures(
            session,
            scope,
            question,
            NOW,
            llm if llm is not None else RecordingLlm(),
            tool_llm,
            playbook if playbook is not None else load_playbook(),
        )


def _refs(answer: FailureAnswer) -> list[str]:
    return [item.ref for item in answer.evidence]


# --- SQ1 spike: tools, evidence, diagnosis ---------------------------------------------------------------


def test_investigate_failures_sq1_spike_is_answered_not_abstained(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    assert answer.abstained is False
    assert (answer.comparison.window.total, answer.comparison.window.failed) == (20, 12)
    assert (answer.comparison.baseline.total, answer.comparison.baseline.failed) == (200, 4)
    assert answer.comparison.elevated is True


def test_investigate_failures_parses_the_question_with_the_rules_when_there_is_no_tool_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    assert answer.query.source == "rules"
    assert answer.query.merchant_id == MERCHANT
    assert (answer.query.start, answer.query.end) == (WINDOW_START, NOW)
    assert (answer.query.method, answer.query.status) == ("card", None)


def test_investigate_failures_records_the_five_tool_calls_in_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    assert [call.name for call in answer.tool_calls] == [
        "compare_baseline",
        "change_timeline",
        "failure_reasons",
        "search_incidents",
        "list_failed_payments",
    ]


def test_investigate_failures_tool_call_arguments_are_strings_for_the_window_and_the_top_bin_query(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    start, end = WINDOW_START.isoformat(), NOW.isoformat()

    # Act
    answer = _run(session_factory)

    # Assert
    calls = {call.name: call.args for call in answer.tool_calls}
    assert calls["compare_baseline"] == {"start": start, "end": end, "baseline_days": "7"}
    assert calls["change_timeline"] == {"start": start, "end": end, "bucket_minutes": "60"}
    assert calls["search_incidents"] == {"query": "BIN 400000 issuer unavailable"}
    assert {key: calls["failure_reasons"][key] for key in ("start", "end")} == {"start": start, "end": end}
    assert calls["failure_reasons"]["method"] == "card"
    assert {key: calls["list_failed_payments"][key] for key in ("start", "end", "limit")} == {
        "start": start,
        "end": end,
        "limit": "5",
    }
    assert all(isinstance(call, ToolCall) for call in answer.tool_calls)


def test_investigate_failures_evidence_follows_the_specified_order_and_refs(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    issuer_failures = _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    newest_five = [f"payment:{payment_id}" for payment_id in reversed(issuer_failures[-5:])]
    assert _refs(answer) == [
        "aggregate:window",
        "aggregate:baseline",
        "comparison:baseline",
        "aggregate:bin:400000",
        "change:failure_onset:card",
        "change:bin_onset:400000",
        "change:new_decline_code:issuer_unavailable",
        "reason:issuer_unavailable",
        "reason:card_declined",
        "incident:inc_sq1",
        *newest_five,
    ]


def test_investigate_failures_every_evidence_item_has_a_non_empty_summary(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    assert [item.summary.strip() != "" for item in answer.evidence] == [True] * len(answer.evidence)


def test_investigate_failures_reason_evidence_summarises_count_share_owner_retryability_and_raw_codes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    summary = next(item.summary for item in answer.evidence if item.ref == "reason:issuer_unavailable")
    for expected in ("10", "issuer", "issuer_unavailable"):
        assert expected in summary
    assert answer.breakdown.total_failed == 12
    assert [(group.taxonomy_code, group.count) for group in answer.breakdown.groups] == [
        ("issuer_unavailable", 10),
        ("card_declined", 2),
    ]


def test_investigate_failures_every_claim_cites_only_evidence_refs_of_this_turn(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    known = set(_refs(answer))
    ungrounded = [claim for claim in answer.claims if not set(claim.evidence_refs) <= known]
    assert answer.claims != ()
    assert ungrounded == []
    assert [claim for claim in answer.claims if claim.evidence_refs == ()] == []


def test_investigate_failures_ranks_the_issuer_unavailable_fix_first_with_the_previously_applied_incident(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    playbook = load_playbook()

    # Act
    answer = _run(session_factory, playbook=playbook)

    # Assert
    fixes = answer.diagnosis.fixes
    assert [(fix.rank, fix.taxonomy_code) for fix in fixes] == [
        (1, "issuer_unavailable"),
        (2, "card_declined"),
    ]
    assert fixes[0].fix == f"{playbook.entries['issuer_unavailable'].fix} Previously applied: {INCIDENT_FIX}"
    assert fixes[0].playbook_version == playbook.version
    assert fixes[0].evidence_refs == ("reason:issuer_unavailable",)
    assert fixes[0].incident_refs == ("incident:inc_sq1",)
    assert fixes[1].fix == playbook.entries["card_declined"].fix
    assert fixes[1].incident_refs == ()


def test_investigate_failures_claims_include_one_fix_claim_per_fix_with_its_evidence_and_incident_refs(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    first, second = answer.diagnosis.fixes
    expected = [
        Claim(text=f"Fix 1: {first.fix}", evidence_refs=("reason:issuer_unavailable", "incident:inc_sq1")),
        Claim(text=f"Fix 2: {second.fix}", evidence_refs=("reason:card_declined",)),
    ]
    assert [claim for claim in answer.claims if claim in expected] == expected


def test_investigate_failures_claims_are_the_grounded_facts_hypotheses_confirmations_and_fixes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    diagnosis = answer.diagnosis
    fix_claims = [
        Claim(text=f"Fix {fix.rank}: {fix.fix}", evidence_refs=(*fix.evidence_refs, *fix.incident_refs))
        for fix in diagnosis.fixes
    ]
    assert answer.claims == (
        *diagnosis.facts,
        *diagnosis.hypotheses,
        *diagnosis.how_to_confirm,
        *fix_claims,
    )


def test_investigate_failures_exposes_the_tool_results_it_built_the_answer_from(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)

    # Act
    answer = _run(session_factory)

    # Assert
    assert [(change.kind, change.subject) for change in answer.timeline.changes] == [
        ("failure_onset", "card"),
        ("bin_onset", "400000"),
        ("new_decline_code", "issuer_unavailable"),
    ]
    assert len(answer.timeline.buckets) == 6
    assert answer.comparison.window.by_bin[0].card_bin == "400000"


# --- narration -------------------------------------------------------------------------------------------


def test_investigate_failures_with_the_mock_llm_answers_in_llm_mode_citing_the_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    llm = RecordingLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.mode == "llm"
    assert answer.model == "mock"
    assert answer.text is not None
    assert [f"[{ref}]" in answer.text for ref in _refs(answer)] == [True] * len(answer.evidence)


def test_investigate_failures_calls_the_llm_once_with_the_question_and_the_answer_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    llm = RecordingLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert llm.calls == [LlmRequest(question=QUESTION, evidence=answer.evidence)]


def test_investigate_failures_closes_the_session_before_calling_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    llm = SessionProbeLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.mode == "llm"
    assert llm.in_transaction == [False]


def test_investigate_failures_keeps_a_narration_that_cites_only_known_refs(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    narration = "The failure rate is far above the baseline [aggregate:window] [comparison:baseline]."
    llm = FixedTextLlm(narration=narration)

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert (answer.mode, answer.text, answer.model) == ("llm", narration, LLM_MODEL)


@pytest.mark.parametrize(
    "narration",
    [
        "Issuer outage [made-up:ref].",
        "The window failed [aggregate:window] because of [incident:inc_made_up].",
        "A narration that cites nothing at all.",
    ],
    ids=["only_an_unknown_ref", "a_known_and_an_unknown_ref", "no_citation"],
)
def test_investigate_failures_drops_a_narration_that_is_not_grounded_and_falls_back_to_t2(
    session_factory: sessionmaker[Session], narration: str
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    llm = FixedTextLlm(narration=narration)

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.mode == "t2_fallback"
    assert answer.text is None
    assert answer.abstained is False
    assert answer.claims != ()
    assert len(llm.calls) == 1


def test_investigate_failures_falls_back_to_t2_without_text_when_the_llm_is_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    llm = DownLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.mode == "t2_fallback"
    assert answer.text is None
    assert answer.abstained is False
    assert len(llm.calls) == 1
    assert [(fix.rank, fix.taxonomy_code) for fix in answer.diagnosis.fixes] == [
        (1, "issuer_unavailable"),
        (2, "card_declined"),
    ]
    assert answer.claims != ()


# --- abstention ------------------------------------------------------------------------------------------


def test_investigate_failures_on_an_empty_database_abstains_with_the_fixed_text_and_no_llm_call(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    llm = RecordingLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.abstained is True
    assert answer.claims == ()
    assert answer.text == ABSTAIN_TEXT
    assert answer.mode == "t2_fallback"
    assert answer.model is None
    assert answer.comparison.window.total == 0
    assert llm.calls == []


def test_investigate_failures_abstains_when_only_the_baseline_has_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _seed_baseline(session, total=20, failed_every=5)
    llm = RecordingLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.abstained is True
    assert (answer.comparison.window.total, answer.comparison.baseline.total) == (0, 20)
    assert (answer.claims, answer.text, answer.mode, answer.model) == ((), ABSTAIN_TEXT, "t2_fallback", None)
    assert llm.calls == []


def test_investigate_failures_abstains_when_all_the_data_belongs_to_another_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory, merchant_id=OTHER_MERCHANT)
    llm = RecordingLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.abstained is True
    assert answer.comparison.window.total == 0
    assert answer.text == ABSTAIN_TEXT
    assert llm.calls == []


def test_investigate_failures_abstains_without_calling_an_unavailable_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: an unavailable LLM must not matter when nothing is to be narrated
    llm = DownLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert (answer.abstained, answer.text, answer.mode) == (True, ABSTAIN_TEXT, "t2_fallback")
    assert llm.calls == []


# --- healthy traffic -------------------------------------------------------------------------------------


def test_investigate_failures_with_failures_within_the_baseline_answers_without_hypotheses_or_fixes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: window 1 failure of 20 (5 %), baseline 10 of 200 (5 %)
    with session_factory.begin() as session:
        _seed_baseline(session, total=200, failed_every=20)
        _fact(
            session,
            WINDOW_START + timedelta(minutes=10),
            state="declined",
            card_bin="411111",
            decline_code="card_declined",
        )
        for index in range(19):
            _fact(session, WINDOW_START + timedelta(minutes=20 + index), state="succeeded", card_bin="411111")
    llm = RecordingLlm()

    # Act
    answer = _run(session_factory, llm)

    # Assert
    assert answer.comparison.elevated is False
    assert answer.abstained is False
    assert answer.diagnosis.hypotheses == ()
    assert answer.diagnosis.how_to_confirm == ()
    assert answer.diagnosis.fixes == ()
    assert answer.mode == "llm"
    assert len(llm.calls) == 1


# --- tool-calling query parsing --------------------------------------------------------------------------


def test_investigate_failures_uses_the_query_the_tool_llm_submits(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    plan = ToolPlan(
        calls=(ToolChoice(name="submit_failure_query", arguments=json.dumps({"lookback_hours": 3})),),
        model="fake",
    )
    tool_llm = FakeToolLlm(plan=plan)
    start = (NOW - timedelta(hours=3)).isoformat()

    # Act
    answer = _run(session_factory, tool_llm=tool_llm)

    # Assert
    assert answer.query.source == "llm"
    assert (answer.query.start, answer.query.end) == (NOW - timedelta(hours=3), NOW)
    assert answer.query.merchant_id == MERCHANT
    assert [tuple(spec.name for spec in request.tools) for request in tool_llm.requests] == [
        ("submit_failure_query",)
    ]
    assert answer.tool_calls[0].args["start"] == start
    assert answer.abstained is False


def test_investigate_failures_falls_back_to_the_rules_when_the_tool_llm_tries_to_set_the_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    arguments = json.dumps({"lookback_hours": 3, "merchant_id": str(OTHER_MERCHANT)})
    plan = ToolPlan(calls=(ToolChoice(name="submit_failure_query", arguments=arguments),), model="fake")

    # Act
    answer = _run(session_factory, tool_llm=FakeToolLlm(plan=plan))

    # Assert
    assert answer.query.source == "rules"
    assert answer.query.merchant_id == MERCHANT
    assert (answer.query.start, answer.query.end) == (WINDOW_START, NOW)


# --- scope -----------------------------------------------------------------------------------------------


def test_investigate_failures_never_shows_another_merchants_payments_reasons_bins_or_incidents(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the other merchant has newer, more numerous failures on its own bin, code and incident
    own_failures = _seed_sq1(session_factory)
    with session_factory.begin() as session:
        other_ids = [
            _fact(
                session,
                NOW - timedelta(minutes=1 + minute),
                state="declined",
                card_bin="499999",
                decline_code="do_not_honor",
                merchant_id=OTHER_MERCHANT,
            )
            for minute in range(30)
        ]
        _incident(session, "inc_other", OTHER_MERCHANT)
    expected_payments = {f"payment:{payment_id}" for payment_id in own_failures[-5:]}

    # Act
    answer = _run(session_factory)

    # Assert
    refs = _refs(answer)
    payment_refs = {ref for ref in refs if ref.startswith("payment:")}
    other_refs = {f"payment:{payment_id}" for payment_id in other_ids}
    assert payment_refs == expected_payments
    assert payment_refs & other_refs == set()
    assert "aggregate:bin:499999" not in refs
    assert "reason:do_not_honor" not in refs
    assert "incident:inc_other" not in refs
    assert (answer.comparison.window.total, answer.comparison.window.failed) == (20, 12)
    assert [item.summary for item in answer.evidence if "499999" in item.summary] == []


def test_investigate_failures_answers_the_other_merchant_from_its_own_data_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_sq1(session_factory)
    other_failures = _seed_sq1(session_factory, merchant_id=OTHER_MERCHANT)

    # Act
    answer = _run(session_factory, scope=Scope(merchant_id=OTHER_MERCHANT))

    # Assert
    refs = _refs(answer)
    assert "incident:inc_other" in refs
    assert "incident:inc_sq1" not in refs
    assert {ref for ref in refs if ref.startswith("payment:")} == {
        f"payment:{payment_id}" for payment_id in other_failures[-5:]
    }
    assert answer.query.merchant_id == OTHER_MERCHANT


def test_investigate_failures_bounds_the_question_before_parsing_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: "yesterday" sits after the 2000-character cut, so it must not move the window
    _seed_sq1(session_factory)
    question = QUESTION + " " + "x" * 2000 + " yesterday"

    # Act
    answer = _run(session_factory, question=question)

    # Assert
    assert (answer.query.start, answer.query.end) == (WINDOW_START, NOW)
