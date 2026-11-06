"""Judge inputs and outputs (docs/specs/evaluation-judge.md sections 6.1 and 6.2)."""

import uuid
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

DisputeState = Literal["opened", "under_review", "won", "lost"]
ActorRole = Literal["card_network", "consumer", "merchant", "ops_lead"]
Verdict = Literal["valid", "needs_review", "invalid"]
LlmStatus = Literal["skipped", "used", "rejected", "unavailable"]
Severity = Literal["hard", "soft"]
CheckName = Literal[
    "transition_sequence",
    "transition_actors",
    "transition_order",
    "outcome_matches_state",
    "evidence_considered",
    "evidence_timing",
    "dispute_amount",
    "chargeback_amounts",
    "reason_code_known",
    "outcome_supported_by_evidence",
]

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class DisputeTransition(BaseModel):
    model_config = _FROZEN
    from_state: DisputeState | None
    to_state: DisputeState
    actor_role: ActorRole
    at: AwareDatetime


class DisputeEvidence(BaseModel):
    model_config = _FROZEN
    evidence_id: str
    kind: str
    submitted_at: AwareDatetime


class DisputeWorkflowRecord(BaseModel):
    model_config = _FROZEN
    dispute_id: uuid.UUID
    payment_id: uuid.UUID
    merchant_id: uuid.UUID
    network_reason_code: str
    currency: str
    payment_amount_minor: int = Field(gt=0)
    refunded_minor: int = Field(ge=0)
    dispute_amount_minor: int
    transitions: tuple[DisputeTransition, ...] = Field(min_length=1)
    evidence: tuple[DisputeEvidence, ...]
    evidence_considered: tuple[str, ...]
    outcome: Literal["won", "lost"] | None
    chargeback_minor: int = Field(ge=0)
    chargeback_fee_minor: int = Field(ge=0)


class Check(BaseModel):
    model_config = _FROZEN
    check: CheckName
    passed: bool
    severity: Severity
    reason: str


class JudgeReason(BaseModel):
    model_config = _FROZEN
    code: CheckName | Literal["llm_concern"]
    detail: str = Field(max_length=300)


class DisputeVerdict(BaseModel):
    model_config = _FROZEN
    dispute_id: uuid.UUID
    verdict: Verdict
    rubric_verdict: Verdict
    checks: tuple[Check, ...]
    reasons: tuple[JudgeReason, ...]
    llm_status: LlmStatus
    model: str | None


class Hypothesis(BaseModel):
    model_config = _FROZEN
    hypothesis_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    cause_code: str = Field(max_length=64)
    statement: str = Field(max_length=500)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=20)


class RankedHypothesis(BaseModel):
    model_config = _FROZEN
    rank: int = Field(ge=1)
    hypothesis_id: str
    cause_code: str
    statement: str
    support_score: float
    supported: bool
    valid_refs: tuple[str, ...]
    fabricated_refs: tuple[str, ...]
    resolution_steps: tuple[str, ...]
    llm_rationale: str | None


class HypothesisRanking(BaseModel):
    model_config = _FROZEN
    ranked: tuple[RankedHypothesis, ...]
    llm_status: LlmStatus
    model: str | None
