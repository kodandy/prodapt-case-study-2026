"""RED tests for investigation root-cause judging, evaluation-judge.md section 6.4.2."""

import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.investigation.service import CauseLabel, Investigation
from ops_ai.investigation.tools import BinShare, FailureAggregate
from ops_ai.judge import root_cause
from ops_ai.judge.fake import FakeJudgeLlm
from ops_ai.judge.hypotheses import PLAYBOOK
from ops_ai.judge.root_cause import (
    JUDGE_TIMEOUT_SECONDS,
    RootCauseAssessment,
    assess_root_cause,
    root_cause_hypothesis,
)
from ops_ai.judge.schemas import Hypothesis, HypothesisRanking

NOW = datetime(2026, 11, 5, 12, tzinfo=UTC)
CARD_BIN = "400000"
WINDOW_REF = "aggregate:window"
BASELINE_REF = "aggregate:baseline"
BIN_REF = f"aggregate:bin:{CARD_BIN}"


def investigation(cause_label: CauseLabel = "issuer_outage_on_bin") -> Investigation:
    return Investigation(
        intent="unsupported" if cause_label == "unsupported" else "failure_spike",
        cause_label=cause_label,
        window=FailureAggregate(
            start=NOW - timedelta(hours=6),
            end=NOW,
            total=100,
            failed=40,
            rate=0.4,
            by_bin=(BinShare(card_bin=CARD_BIN, failed=30, total=50),),
            by_code=(),
        ),
        baseline=FailureAggregate(
            start=NOW - timedelta(days=7, hours=6),
            end=NOW - timedelta(hours=6),
            total=1000,
            failed=10,
            rate=0.01,
            by_bin=(),
            by_code=(),
        ),
        suggested_fixes=("Retry through the secondary acquirer.",),
        tool_calls=(),
        evidence=(
            EvidenceItem(ref=BIN_REF, summary=f"BIN {CARD_BIN}: 30 failed payments."),
            EvidenceItem(
                ref="incident:inc_1", summary="Issuer outage. Fix: Route to the secondary acquirer."
            ),
            EvidenceItem(ref=BASELINE_REF, summary="Baseline: 10 of 1000 payments failed."),
            EvidenceItem(ref=WINDOW_REF, summary="Window: 40 of 100 payments failed."),
            EvidenceItem(ref="payment:payment_1", summary="Payment failed on the affected BIN."),
        ),
        mode="t2_fallback",
        text=None,
        model=None,
    )


def skipped_assessment(reason: str) -> RootCauseAssessment:
    return RootCauseAssessment(
        status="skipped",
        reason=reason,
        cause_code=None,
        score=None,
        passed=None,
        rationale=None,
        resolution_steps=(),
        llm_status=None,
        model=None,
    )


@dataclass
class UnavailableJudgeLlm:
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        raise LlmUnavailableError("provider unavailable")


@dataclass
class InvalidJsonJudgeLlm:
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(text="this is not JSON", model="invalid-json-judge")


@dataclass
class ExplodingJudgeLlm:
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        raise RuntimeError("internal judge failure")


@dataclass
class SlowJudgeLlm:
    requests: list[LlmRequest] = field(default_factory=list)
    sleep_seconds: float = 0.8

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        time.sleep(self.sleep_seconds)
        return LlmResponse(
            text='{"order": ["route_cause"], "rationales": {}}',
            model="slow-judge",
        )


@dataclass
class ThreadRecordingJudgeLlm:
    judge: FakeJudgeLlm = field(default_factory=FakeJudgeLlm)
    thread_ids: list[int] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.thread_ids.append(threading.get_ident())
        return self.judge.complete(request)


def test_default_judge_timeout_is_five_seconds() -> None:
    # Arrange
    expected = 5.0

    # Act
    actual = JUDGE_TIMEOUT_SECONDS

    # Assert
    assert actual == expected


@pytest.mark.parametrize("cause_label", ["unsupported", "no_anomaly"])
def test_hypothesis_is_absent_for_unjudgeable_causes(cause_label: CauseLabel) -> None:
    # Arrange
    result = investigation(cause_label)

    # Act
    hypothesis = root_cause_hypothesis(result)

    # Assert
    assert hypothesis is None
    assert result.cause_label == cause_label


def test_issuer_hypothesis_cites_aggregate_refs_in_spec_order_and_names_the_bin() -> None:
    # Arrange
    result = investigation()

    # Act
    hypothesis = root_cause_hypothesis(result)

    # Assert
    assert isinstance(hypothesis, Hypothesis)
    assert hypothesis.hypothesis_id == "route_cause"
    assert hypothesis.cause_code == "issuer_outage_on_bin"
    assert hypothesis.evidence_refs == (WINDOW_REF, BASELINE_REF, BIN_REF)
    assert CARD_BIN in hypothesis.statement


def test_issuer_hypothesis_reads_the_bin_from_evidence_without_window_or_baseline_objects() -> None:
    # Arrange
    result = replace(
        investigation(),
        window=None,
        baseline=None,
        evidence=(
            EvidenceItem(ref="aggregate:bin:555555", summary="BIN 555555 has elevated failures."),
            EvidenceItem(ref=BASELINE_REF, summary="Baseline failures."),
            EvidenceItem(ref=WINDOW_REF, summary="Window failures."),
        ),
    )

    # Act
    hypothesis = root_cause_hypothesis(result)

    # Assert
    assert isinstance(hypothesis, Hypothesis)
    assert hypothesis.hypothesis_id == "route_cause"
    assert hypothesis.cause_code == "issuer_outage_on_bin"
    assert hypothesis.evidence_refs == (WINDOW_REF, BASELINE_REF, "aggregate:bin:555555")
    assert "555555" in hypothesis.statement


def test_issuer_hypothesis_does_not_fabricate_a_bin_ref_absent_from_evidence() -> None:
    # Arrange
    result = investigation()
    result = replace(result, evidence=tuple(item for item in result.evidence if item.ref != BIN_REF))

    # Act
    hypothesis = root_cause_hypothesis(result)

    # Assert
    assert isinstance(hypothesis, Hypothesis)
    assert hypothesis.hypothesis_id == "route_cause"
    assert hypothesis.cause_code == "issuer_outage_on_bin"
    assert hypothesis.evidence_refs == (WINDOW_REF, BASELINE_REF)


def test_elevated_failure_hypothesis_omits_bin_incident_and_payment_refs() -> None:
    # Arrange
    result = investigation("elevated_failures")

    # Act
    hypothesis = root_cause_hypothesis(result)

    # Assert
    assert isinstance(hypothesis, Hypothesis)
    assert hypothesis.hypothesis_id == "route_cause"
    assert hypothesis.cause_code == "elevated_failures"
    assert hypothesis.evidence_refs == (WINDOW_REF, BASELINE_REF)
    assert hypothesis.statement.strip() != ""


@pytest.mark.parametrize("cause_label", ["issuer_outage_on_bin", "elevated_failures"])
def test_hypothesis_uses_a_fixed_statement_independent_of_narration(cause_label: CauseLabel) -> None:
    # Arrange
    original = investigation(cause_label)
    narrated = replace(original, text="Untrusted alternative explanation.", model="answer-model", mode="llm")

    # Act
    original_hypothesis = root_cause_hypothesis(original)
    narrated_hypothesis = root_cause_hypothesis(narrated)

    # Assert
    assert isinstance(original_hypothesis, Hypothesis)
    assert narrated_hypothesis == original_hypothesis
    assert "Untrusted alternative explanation." not in original_hypothesis.statement


@pytest.mark.parametrize("cause_label", ["unsupported", "no_anomaly"])
@pytest.mark.parametrize("configured", [False, True])
def test_no_hypothesis_skips_before_checking_or_calling_judge(
    cause_label: CauseLabel, configured: bool
) -> None:
    # Arrange
    result = investigation(cause_label)
    llm = FakeJudgeLlm()
    judge_llm = llm if configured else None

    # Act
    assessment = assess_root_cause(result, judge_llm)

    # Assert
    assert assessment == skipped_assessment("no root-cause hypothesis to judge")
    assert llm.requests == []


def test_unconfigured_judge_skips_without_ranking_or_llm_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    result = investigation()
    calls: list[tuple[Sequence[Hypothesis], Sequence[EvidenceItem], LlmPort | None]] = []

    def unexpected_rank(
        hypotheses: Sequence[Hypothesis], evidence: Sequence[EvidenceItem], llm: LlmPort | None = None
    ) -> HypothesisRanking:
        calls.append((hypotheses, evidence, llm))
        raise AssertionError("ranking must not run without a configured judge")

    monkeypatch.setattr(root_cause, "rank_hypotheses", unexpected_rank)

    # Act
    assessment = assess_root_cause(result, None)

    # Assert
    assert assessment == skipped_assessment("no judge LLM configured")
    assert calls == []


@pytest.mark.parametrize(
    ("cause_label", "expected_score"),
    [("issuer_outage_on_bin", 6.0), ("elevated_failures", 3.0)],
)
def test_fake_judge_preserves_rubric_score_support_and_playbook(
    cause_label: CauseLabel, expected_score: float
) -> None:
    # Arrange
    result = investigation(cause_label)
    llm = FakeJudgeLlm()

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment == RootCauseAssessment(
        status="assessed",
        reason=None,
        cause_code=cause_label,
        score=expected_score,
        passed=True,
        rationale=None,
        resolution_steps=PLAYBOOK[cause_label],
        llm_status="used",
        model="fake-judge",
    )
    assert len(llm.requests) == 1
    assert llm.requests[0].evidence == result.evidence


def test_missing_baseline_evidence_marks_hypothesis_unsupported_despite_positive_score() -> None:
    # Arrange
    result = investigation()
    result = replace(result, evidence=tuple(item for item in result.evidence if item.ref != BASELINE_REF))
    llm = FakeJudgeLlm()

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment == RootCauseAssessment(
        status="assessed",
        reason=None,
        cause_code="issuer_outage_on_bin",
        score=3.0,
        passed=False,
        rationale=None,
        resolution_steps=PLAYBOOK["issuer_outage_on_bin"],
        llm_status="used",
        model="fake-judge",
    )
    assert len(llm.requests) == 1


def test_assessment_copies_judge_rationale_without_changing_deterministic_score() -> None:
    # Arrange
    result = investigation()
    rationale = "The window, baseline and affected BIN support this cause."
    llm = FakeJudgeLlm(reply=json.dumps({"order": ["route_cause"], "rationales": {"route_cause": rationale}}))

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment == RootCauseAssessment(
        status="assessed",
        reason=None,
        cause_code="issuer_outage_on_bin",
        score=6.0,
        passed=True,
        rationale=rationale,
        resolution_steps=PLAYBOOK["issuer_outage_on_bin"],
        llm_status="used",
        model="fake-judge",
    )
    assert len(llm.requests) == 1


def test_unavailable_llm_still_returns_deterministic_assessment() -> None:
    # Arrange
    result = investigation()
    llm = UnavailableJudgeLlm()

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment == RootCauseAssessment(
        status="assessed",
        reason=None,
        cause_code="issuer_outage_on_bin",
        score=6.0,
        passed=True,
        rationale=None,
        resolution_steps=PLAYBOOK["issuer_outage_on_bin"],
        llm_status="unavailable",
        model=None,
    )
    assert len(llm.requests) == 1
    assert llm.requests[0].evidence == result.evidence


def test_invalid_json_reply_is_rejected_without_losing_deterministic_assessment() -> None:
    # Arrange
    result = investigation()
    llm = InvalidJsonJudgeLlm()

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment == RootCauseAssessment(
        status="assessed",
        reason=None,
        cause_code="issuer_outage_on_bin",
        score=6.0,
        passed=True,
        rationale=None,
        resolution_steps=PLAYBOOK["issuer_outage_on_bin"],
        llm_status="rejected",
        model="invalid-json-judge",
    )
    assert len(llm.requests) == 1


def test_unexpected_judge_exception_is_skipped_with_sanitized_reason() -> None:
    # Arrange
    result = investigation()
    llm = ExplodingJudgeLlm()

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment == skipped_assessment("judge failed")
    assert len(llm.requests) == 1


def test_judge_runs_on_a_worker_thread() -> None:
    # Arrange
    result = investigation()
    llm = ThreadRecordingJudgeLlm()
    caller_thread = threading.get_ident()

    # Act
    assessment = assess_root_cause(result, llm)

    # Assert
    assert assessment.status == "assessed"
    assert assessment.llm_status == "used"
    assert assessment.model == "fake-judge"
    assert len(llm.thread_ids) == 1
    assert [thread_id == caller_thread for thread_id in llm.thread_ids] == [False]
    assert len(llm.judge.requests) == 1


def test_slow_judge_returns_timeout_without_waiting_for_the_llm_to_finish() -> None:
    # Arrange
    result = investigation()
    llm = SlowJudgeLlm()
    started = time.monotonic()

    # Act
    assessment = assess_root_cause(result, llm, timeout_seconds=0.05)
    elapsed = time.monotonic() - started

    # Assert
    assert assessment == skipped_assessment("judge timed out")
    assert len(llm.requests) == 1
    assert elapsed < llm.sleep_seconds / 2
