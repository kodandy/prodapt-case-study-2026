"""Dispute workflow validation: rubric first, LLM second (docs/specs/evaluation-judge.md 6.1)."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.judge.fake import RUBRIC_JSON_PREFIX
from ops_ai.judge.llm_parse import parse_reply
from ops_ai.judge.schemas import (
    ActorRole,
    Check,
    CheckName,
    DisputeState,
    DisputeVerdict,
    DisputeWorkflowRecord,
    JudgeReason,
    LlmStatus,
    Severity,
    Verdict,
)

__all__ = ["REASON_CODES", "JudgeReason", "ReasonCode", "judge_dispute", "rubric_checks"]


@dataclass(frozen=True)
class ReasonCode:
    category: str
    compelling_evidence: frozenset[str]


REASON_CODES: Mapping[str, ReasonCode] = {
    "10.4": ReasonCode(
        "fraud_card_absent",
        frozenset({"avs_cvv_match", "3ds_authentication", "prior_undisputed_history", "device_ip_match"}),
    ),
    "12.6.1": ReasonCode("duplicate_processing", frozenset({"distinct_transactions_proof", "refund_issued"})),
    "13.1": ReasonCode("not_received", frozenset({"proof_of_delivery", "tracking_confirmation"})),
    "13.2": ReasonCode(
        "cancelled_recurring", frozenset({"cancellation_policy_accepted", "usage_after_cancellation"})
    ),
    "13.3": ReasonCode("not_as_described", frozenset({"product_description", "return_policy_accepted"})),
    "13.6": ReasonCode("credit_not_processed", frozenset({"refund_issued", "return_policy_accepted"})),
}

_LEGAL: frozenset[tuple[DisputeState | None, DisputeState]] = frozenset(
    {(None, "opened"), ("opened", "under_review"), ("under_review", "won"), ("under_review", "lost")}
)
_ACTORS: Mapping[DisputeState, frozenset[ActorRole]] = {
    "opened": frozenset({"card_network", "consumer"}),
    "under_review": frozenset({"merchant"}),
    "won": frozenset({"card_network", "ops_lead"}),
    "lost": frozenset({"card_network", "ops_lead"}),
}
_RESOLVED: frozenset[DisputeState] = frozenset({"won", "lost"})
_SEVERITY: Mapping[CheckName, Severity] = {
    "transition_sequence": "hard",
    "transition_actors": "hard",
    "transition_order": "hard",
    "outcome_matches_state": "hard",
    "evidence_considered": "hard",
    "evidence_timing": "hard",
    "dispute_amount": "hard",
    "chargeback_amounts": "hard",
    "reason_code_known": "soft",
    "outcome_supported_by_evidence": "soft",
}
_MAX_DETAIL = 300

JUDGE_INSTRUCTIONS = (
    "You are a dispute workflow judge. A deterministic rubric already checked the record below. Look for "
    "anything the rubric cannot see (inconsistent evidence, implausible timing). You may not change the "
    "record or its scope. Reply with JSON only: "
    '{"verdict": "valid"|"needs_review"|"invalid", "reasons": [{"code": "llm_concern", "detail": "<=300 '
    'characters"}]} with at most 10 reasons.'
)


class _LlmReason(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: Literal["llm_concern"]
    detail: StrictStr = Field(max_length=_MAX_DETAIL)


class _LlmVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    verdict: Verdict
    reasons: tuple[_LlmReason, ...] = Field(max_length=10)


def _check(name: CheckName, failures: list[str], ok_reason: str) -> Check:
    passed = not failures
    return Check(
        check=name,
        passed=passed,
        severity=_SEVERITY[name],
        reason=ok_reason if passed else "; ".join(failures),
    )


def _sequence(record: DisputeWorkflowRecord) -> Check:
    failures: list[str] = []
    first = record.transitions[0]
    if (first.from_state, first.to_state) != (None, "opened"):
        failures.append(
            f"first transition is {first.from_state} -> {first.to_state}, expected None -> opened"
        )
    for index, transition in enumerate(record.transitions):
        if index > 0 and transition.from_state != record.transitions[index - 1].to_state:
            failures.append(f"transition {index} starts from {transition.from_state}, not the previous state")
        if (transition.from_state, transition.to_state) not in _LEGAL:
            failures.append(f"transition {index} {transition.from_state} -> {transition.to_state} is illegal")
    return _check("transition_sequence", failures, "every transition is legal and chained")


def _actors(record: DisputeWorkflowRecord) -> Check:
    failures = [
        f"transition {index} to {t.to_state} by {t.actor_role} is not allowed"
        for index, t in enumerate(record.transitions)
        if t.actor_role not in _ACTORS[t.to_state]
    ]
    return _check("transition_actors", failures, "every transition is by an allowed actor")


def _order(record: DisputeWorkflowRecord) -> Check:
    failures = [
        f"transition {index} happens before transition {index - 1}"
        for index in range(1, len(record.transitions))
        if record.transitions[index].at < record.transitions[index - 1].at
    ]
    return _check("transition_order", failures, "transition times are non-decreasing")


def _final_state(record: DisputeWorkflowRecord) -> DisputeState:
    return record.transitions[-1].to_state


def _resolution_time(record: DisputeWorkflowRecord) -> datetime | None:
    return record.transitions[-1].at if _final_state(record) in _RESOLVED else None


def _outcome(record: DisputeWorkflowRecord) -> Check:
    final = _final_state(record)
    expected = final if final in _RESOLVED else None
    failures = [] if record.outcome == expected else [f"outcome {record.outcome} but final state is {final}"]
    return _check("outcome_matches_state", failures, "outcome matches the final state")


def _considered(record: DisputeWorkflowRecord) -> Check:
    submitted = {item.evidence_id for item in record.evidence}
    considered = set(record.evidence_considered)
    failures: list[str] = []
    unknown = sorted(considered - submitted)
    if unknown:
        failures.append(f"{len(unknown)} considered evidence id(s) were never submitted")
    if _final_state(record) in _RESOLVED:
        ignored = sorted(submitted - considered)
        if ignored:
            failures.append(
                f"{len(ignored)} submitted evidence item(s) were not considered: {', '.join(ignored)}"
            )
    return _check("evidence_considered", failures, "considered evidence matches the submitted evidence")


def _timing(record: DisputeWorkflowRecord) -> Check:
    opened_at = record.transitions[0].at
    resolved_at = _resolution_time(record)
    failures: list[str] = []
    for item in record.evidence:
        if item.submitted_at < opened_at:
            failures.append(f"evidence {item.evidence_id} was submitted before the dispute opened")
        elif resolved_at is not None and item.submitted_at > resolved_at:
            failures.append(f"evidence {item.evidence_id} was submitted after the resolution")
    return _check("evidence_timing", failures, "evidence was submitted while the dispute was open")


def _dispute_amount(record: DisputeWorkflowRecord) -> Check:
    refundable = record.payment_amount_minor - record.refunded_minor
    failures = (
        []
        if 0 < record.dispute_amount_minor <= refundable
        else [f"dispute amount {record.dispute_amount_minor} is outside (0, {refundable}]"]
    )
    return _check("dispute_amount", failures, "dispute amount fits the refundable amount")


def _chargeback(record: DisputeWorkflowRecord) -> Check:
    failures: list[str] = []
    if record.outcome == "lost":
        if record.chargeback_minor != record.dispute_amount_minor:
            failures.append(
                f"lost dispute charged back {record.chargeback_minor}, expected {record.dispute_amount_minor}"
            )
        if record.chargeback_fee_minor <= 0:
            failures.append("lost dispute has no chargeback fee")
    elif record.chargeback_minor != 0 or record.chargeback_fee_minor != 0:
        failures.append("chargeback or fee recorded on a dispute that was not lost")
    return _check("chargeback_amounts", failures, "chargeback amounts match the outcome")


def _reason_code(record: DisputeWorkflowRecord) -> Check:
    failures = (
        []
        if record.network_reason_code in REASON_CODES
        else [f"reason code {record.network_reason_code!r} is not in the catalog"]
    )
    return _check("reason_code_known", failures, "reason code is in the catalog")


def _supported(record: DisputeWorkflowRecord) -> Check:
    reason_code = REASON_CODES.get(record.network_reason_code)
    failures: list[str] = []
    if reason_code is not None and record.outcome is not None:
        considered = set(record.evidence_considered)
        compelling = sorted(
            {
                item.kind
                for item in record.evidence
                if item.evidence_id in considered and item.kind in reason_code.compelling_evidence
            }
        )
        if record.outcome == "won" and not compelling:
            failures.append(f"won without compelling evidence for reason code {record.network_reason_code}")
        if record.outcome == "lost" and compelling:
            failures.append(f"lost despite compelling evidence: {', '.join(compelling)}")
    return _check("outcome_supported_by_evidence", failures, "outcome is consistent with the evidence")


def rubric_checks(record: DisputeWorkflowRecord) -> tuple[Check, ...]:
    return (
        _sequence(record),
        _actors(record),
        _order(record),
        _outcome(record),
        _considered(record),
        _timing(record),
        _dispute_amount(record),
        _chargeback(record),
        _reason_code(record),
        _supported(record),
    )


def _rubric_verdict(checks: tuple[Check, ...]) -> Verdict:
    failed = {check.severity for check in checks if not check.passed}
    if "hard" in failed:
        return "invalid"
    return "needs_review" if "soft" in failed else "valid"


def _prompt(record: DisputeWorkflowRecord, checks: tuple[Check, ...], rubric_verdict: Verdict) -> str:
    agreeing = json.dumps({"verdict": rubric_verdict, "reasons": []})
    rubric = json.dumps([check.model_dump() for check in checks])
    return "\n".join(
        (
            JUDGE_INSTRUCTIONS,
            f"RECORD: {record.model_dump_json()}",
            f"RUBRIC_CHECKS: {rubric}",
            f"{RUBRIC_JSON_PREFIX} {agreeing}",
        )
    )


def judge_dispute(record: DisputeWorkflowRecord, llm: LlmPort | None = None) -> DisputeVerdict:
    checks = rubric_checks(record)
    rubric_verdict = _rubric_verdict(checks)
    reasons = [
        JudgeReason(code=check.check, detail=check.reason[:_MAX_DETAIL])
        for check in checks
        if not check.passed
    ]
    verdict = rubric_verdict
    status: LlmStatus = "skipped"
    model: str | None = None
    if llm is not None:
        try:
            response = llm.complete(LlmRequest(question=_prompt(record, checks, rubric_verdict), evidence=()))
        except LlmUnavailableError:
            status = "unavailable"
        else:
            model = response.model
            reply = parse_reply(response.text, _LlmVerdict)
            if reply is None:
                status = "rejected"
            else:
                status = "used"
                reasons.extend(JudgeReason(code="llm_concern", detail=item.detail) for item in reply.reasons)
                if rubric_verdict == "valid" and reply.verdict != "valid":
                    verdict = "needs_review"
    return DisputeVerdict(
        dispute_id=record.dispute_id,
        verdict=verdict,
        rubric_verdict=rubric_verdict,
        checks=checks,
        reasons=tuple(reasons),
        llm_status=status,
        model=model,
    )
