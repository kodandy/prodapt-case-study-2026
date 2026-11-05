"""Judge assessment of the investigation route's root cause (docs/specs/evaluation-judge.md section 6.4.2)."""

import concurrent.futures
from dataclasses import dataclass
from typing import Literal

from ops_ai.assistant.llm_port import LlmPort
from ops_ai.investigation.service import Investigation
from ops_ai.judge.hypotheses import rank_hypotheses
from ops_ai.judge.schemas import Hypothesis, LlmStatus

__all__ = [
    "JUDGE_TIMEOUT_SECONDS",
    "RootCauseAssessment",
    "RootCauseJudgeStatus",
    "assess_root_cause",
    "root_cause_hypothesis",
]

JUDGE_TIMEOUT_SECONDS = 5.0
HYPOTHESIS_ID = "route_cause"
_BIN_PREFIX = "aggregate:bin:"

RootCauseJudgeStatus = Literal["assessed", "skipped"]

# Shared so a timed-out call (which cannot be cancelled) never blocks a later request's thread creation.
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="root-cause-judge")


@dataclass(frozen=True)
class RootCauseAssessment:
    status: RootCauseJudgeStatus
    reason: str | None
    cause_code: str | None
    score: float | None
    passed: bool | None
    rationale: str | None
    resolution_steps: tuple[str, ...]
    llm_status: LlmStatus | None
    model: str | None


def _skipped(reason: str) -> RootCauseAssessment:
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


def root_cause_hypothesis(investigation: Investigation) -> Hypothesis | None:
    """The route's own cause label as a hypothesis citing the evidence its rule used."""
    refs = ["aggregate:window", "aggregate:baseline"]
    if investigation.cause_label == "elevated_failures":
        statement = (
            "Card failures are at least twice the 7-day baseline and no single BIN carries half of them."
        )
    elif investigation.cause_label == "issuer_outage_on_bin":
        bin_refs = [item.ref for item in investigation.evidence if item.ref.startswith(_BIN_PREFIX)]
        bin_ref = bin_refs[0] if bin_refs else None
        card_bin = "unknown" if bin_ref is None else bin_ref.removeprefix(_BIN_PREFIX)
        if bin_ref is not None:
            refs.append(bin_ref)
        statement = (
            f"The issuer behind BIN {card_bin} is failing: the failure rate is at least twice the 7-day "
            "baseline and this BIN carries at least half of the failures."
        )
    else:
        return None
    return Hypothesis(
        hypothesis_id=HYPOTHESIS_ID,
        cause_code=investigation.cause_label,
        statement=statement,
        evidence_refs=tuple(refs),
    )


def assess_root_cause(
    investigation: Investigation,
    judge_llm: LlmPort | None,
    timeout_seconds: float = JUDGE_TIMEOUT_SECONDS,
) -> RootCauseAssessment:
    """Never raises and never waits longer than `timeout_seconds` for the judge."""
    hypothesis = root_cause_hypothesis(investigation)
    if hypothesis is None:
        return _skipped("no root-cause hypothesis to judge")
    if judge_llm is None:
        return _skipped("no judge LLM configured")
    future = _EXECUTOR.submit(rank_hypotheses, (hypothesis,), investigation.evidence, judge_llm)
    try:
        ranking = future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError:
        future.cancel()
        return _skipped("judge timed out")
    except Exception:
        return _skipped("judge failed")
    ranked = ranking.ranked[0]
    return RootCauseAssessment(
        status="assessed",
        reason=None,
        cause_code=ranked.cause_code,
        score=ranked.support_score,
        passed=ranked.supported,
        rationale=ranked.llm_rationale,
        resolution_steps=ranked.resolution_steps,
        llm_status=ranking.llm_status,
        model=ranking.model,
    )
