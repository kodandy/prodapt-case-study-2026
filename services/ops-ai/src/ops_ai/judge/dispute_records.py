"""Postgres DisputeRecordSource over the gw.disputes.v1 projection (docs/specs/evaluation-judge.md 6.4.1).

The gateway models evidence as one free-text submission with no kind, so each dispute.evidence_submitted step
becomes one evidence item of GATEWAY_EVIDENCE_KIND (not compelling for any catalog reason code). A dispute
opened with a free-text reason has no network reason code and maps to UNSPECIFIED_REASON_CODE.
"""

import uuid
from collections import defaultdict
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.ingest.models import DisputeWorkflow, DisputeWorkflowStep
from ops_ai.judge.schemas import (
    ActorRole,
    DisputeEvidence,
    DisputeState,
    DisputeTransition,
    DisputeWorkflowRecord,
)
from ops_ai.scope import Scope

__all__ = ["GATEWAY_EVIDENCE_KIND", "UNSPECIFIED_REASON_CODE", "PostgresDisputeRecordSource"]

GATEWAY_EVIDENCE_KIND = "merchant_statement"
UNSPECIFIED_REASON_CODE = "unspecified"
_EVIDENCE_EVENT = "dispute.evidence_submitted"
_STATES: dict[str, DisputeState] = {
    "opened": "opened",
    "under_review": "under_review",
    "won": "won",
    "lost": "lost",
}
_ACTORS: dict[str, ActorRole] = {
    "card_network": "card_network",
    "consumer": "consumer",
    "merchant": "merchant",
    "ops_lead": "ops_lead",
}
_OUTCOMES: dict[str, Literal["won", "lost"]] = {"won": "won", "lost": "lost"}


class PostgresDisputeRecordSource:
    """Reads only scope.merchant_id's workflows, most recently updated first. A workflow whose steps
    1..entity_version have not all arrived yet is left out rather than judged on a partial history."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def dispute_records(self, scope: Scope, limit: int) -> Sequence[DisputeWorkflowRecord]:
        with self._session_factory() as session:
            workflows = list(
                session.execute(
                    select(DisputeWorkflow)
                    .where(DisputeWorkflow.merchant_id == scope.merchant_id)
                    .order_by(DisputeWorkflow.updated_at.desc(), DisputeWorkflow.dispute_id)
                    .limit(limit)
                ).scalars()
            )
            if not workflows:
                return []
            steps_by_dispute: dict[uuid.UUID, list[DisputeWorkflowStep]] = defaultdict(list)
            for step in session.execute(
                select(DisputeWorkflowStep)
                .where(DisputeWorkflowStep.dispute_id.in_([workflow.dispute_id for workflow in workflows]))
                .order_by(DisputeWorkflowStep.dispute_id, DisputeWorkflowStep.entity_version)
            ).scalars():
                steps_by_dispute[step.dispute_id].append(step)
            records: list[DisputeWorkflowRecord] = []
            for workflow in workflows:
                steps = steps_by_dispute[workflow.dispute_id]
                if [step.entity_version for step in steps] != list(range(1, workflow.entity_version + 1)):
                    continue
                records.append(_record(workflow, steps))
            return records


def _record(workflow: DisputeWorkflow, steps: Sequence[DisputeWorkflowStep]) -> DisputeWorkflowRecord:
    transitions = tuple(
        DisputeTransition(
            from_state=None if step.from_state is None else _STATES[step.from_state],
            to_state=_STATES[step.to_state],
            actor_role=_ACTORS[step.actor_role],
            at=step.occurred_at,
        )
        for step in steps
    )
    evidence = tuple(
        DisputeEvidence(
            evidence_id=str(step.event_id), kind=GATEWAY_EVIDENCE_KIND, submitted_at=step.occurred_at
        )
        for step in steps
        if step.event_type == _EVIDENCE_EVENT
    )
    outcome = _OUTCOMES.get(steps[-1].to_state)
    return DisputeWorkflowRecord(
        dispute_id=workflow.dispute_id,
        payment_id=workflow.payment_id,
        merchant_id=workflow.merchant_id,
        network_reason_code=workflow.network_reason_code or UNSPECIFIED_REASON_CODE,
        currency=workflow.currency,
        payment_amount_minor=workflow.payment_amount_minor,
        refunded_minor=workflow.refunded_minor,
        dispute_amount_minor=workflow.dispute_amount_minor,
        transitions=transitions,
        evidence=evidence,
        evidence_considered=tuple(item.evidence_id for item in evidence) if outcome is not None else (),
        outcome=outcome,
        chargeback_minor=workflow.chargeback_minor,
        chargeback_fee_minor=workflow.chargeback_fee_minor,
    )
