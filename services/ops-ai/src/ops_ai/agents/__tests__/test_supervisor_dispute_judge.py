"""RED tests for dispute-agent judge wiring in docs/specs/evaluation-judge.md section 6.4.1."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ops_ai.agents.messages import AgentContribution, JsonScalar
from ops_ai.agents.supervisor import AgentInvestigation, run_agents
from ops_ai.agents.tools import AGENT_TOOLS
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.judge.dispute import judge_dispute
from ops_ai.judge.dispute_summary import MAX_JUDGED_DISPUTES, DisputeJudgement
from ops_ai.judge.schemas import DisputeWorkflowRecord
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
DISPUTE_ID = uuid.UUID("00000000-0000-4000-8000-000000000101")
SCOPE = Scope(merchant_id=MERCHANT)
QUESTION = "Any dispute open?"

INSERT_DISPUTE_TICKET = text(
    "INSERT INTO ops_ai.tickets (ticket_id, source_key, source, queue, priority, merchant_id, title, reason, "
    "created_at) VALUES (:ticket_id, :source_key, 'dispute', 'payments_ops', 'normal', :merchant_id, "
    "'Chargeback received', 'Chargeback on a payment', :created_at)"
)


@dataclass
class StubDisputeRecordSource:
    records: tuple[DisputeWorkflowRecord, ...]
    calls: list[tuple[Scope, int]] = field(default_factory=list)

    def dispute_records(self, scope: Scope, limit: int) -> tuple[DisputeWorkflowRecord, ...]:
        self.calls.append((scope, limit))
        return self.records


def _record(*, merchant_id: uuid.UUID = MERCHANT, invalid: bool = False) -> DisputeWorkflowRecord:
    return DisputeWorkflowRecord.model_validate(
        {
            "dispute_id": DISPUTE_ID,
            "payment_id": "00000000-0000-4000-8000-000000000201",
            "merchant_id": merchant_id,
            "network_reason_code": "13.1",
            "currency": "USD",
            "payment_amount_minor": 12000,
            "refunded_minor": 0,
            "dispute_amount_minor": 12000,
            "transitions": [
                {
                    "from_state": None,
                    "to_state": "opened",
                    "actor_role": "card_network",
                    "at": "2026-10-01T09:00:00Z",
                },
                {
                    "from_state": "opened",
                    "to_state": "under_review",
                    "actor_role": "merchant",
                    "at": "2026-10-02T10:00:00Z",
                },
                {
                    "from_state": "under_review",
                    "to_state": "won",
                    "actor_role": "merchant" if invalid else "card_network",
                    "at": "2026-10-10T12:00:00Z",
                },
            ],
            "evidence": [
                {
                    "evidence_id": "ev_1",
                    "kind": "proof_of_delivery",
                    "submitted_at": "2026-10-02T10:00:00Z",
                }
            ],
            "evidence_considered": ["ev_1"],
            "outcome": "won",
            "chargeback_minor": 0,
            "chargeback_fee_minor": 0,
        }
    )


def _dispute_ticket(session_factory: sessionmaker[Session]) -> None:
    ticket_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_DISPUTE_TICKET,
            {
                "ticket_id": ticket_id,
                "source_key": f"dispute:{ticket_id}",
                "merchant_id": MERCHANT,
                "created_at": NOW,
            },
        )


def _dispute(result: AgentInvestigation) -> AgentContribution:
    return next(contribution for contribution in result.contributions if contribution.agent == "dispute")


def _finding_payload(result: AgentInvestigation) -> dict[str, JsonScalar]:
    payloads = [
        dict(message.payload)
        for message in result.messages
        if message.sender == "dispute" and message.recipient == "supervisor" and message.intent == "finding"
    ]
    assert len(payloads) == 1
    return payloads[0]


def test_missing_source_skips_judging_and_preserves_ticket_evidence_and_tool_calls(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _dispute_ticket(session_factory)

    # Act
    with session_factory.begin() as session:
        result = run_agents(session, SCOPE, QUESTION, NOW)

    # Assert
    dispute = _dispute(result)
    assert dispute.judge == DisputeJudgement(
        status="skipped",
        reason="no dispute workflow source is configured",
        disputes=0,
        verdict=None,
        score=None,
        passed=None,
        rationale=None,
        dispute_ids=(),
    )
    assert dispute.status == "finding"
    assert dispute.summary == (
        "1 dispute ticket(s) are on record and the latest statement carries 0 in chargebacks (minor units)."
    )
    assert dispute.evidence == (
        EvidenceItem(
            ref=f"disputes:{MERCHANT}",
            summary=(
                "1 dispute ticket(s) (1 open, 0 assigned, 0 resolved); "
                "chargebacks 0 and chargeback fees 0 (minor units) in statement none"
            ),
        ),
    )
    assert result.evidence == dispute.evidence
    assert [call.name for call in dispute.tool_calls] == ["dispute_tickets"]
    assert _finding_payload(result) == {
        "status": "finding",
        "judge_status": "skipped",
        "judge_reason": "no dispute workflow source is configured",
        "judge_disputes": 0,
        "judge_verdict": None,
        "judge_score": None,
        "judge_passed": None,
        "judge_rationale": None,
    }


def test_valid_record_appends_judge_evidence_and_reports_valid_payload_without_a_new_tool_call(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _dispute_ticket(session_factory)
    source = StubDisputeRecordSource((_record(),))

    # Act
    with session_factory.begin() as session:
        result = run_agents(session, SCOPE, QUESTION, NOW, dispute_records=source)

    # Assert
    dispute = _dispute(result)
    assert source.calls == [(SCOPE, MAX_JUDGED_DISPUTES)]
    assert dispute.judge == DisputeJudgement(
        status="judged",
        reason=None,
        disputes=1,
        verdict="valid",
        score=1.0,
        passed=True,
        rationale="1 dispute workflow(s) pass the rubric",
        dispute_ids=(DISPUTE_ID,),
    )
    assert dispute.status == "finding"
    assert [item.ref for item in dispute.evidence] == [f"disputes:{MERCHANT}", f"dispute_judge:{MERCHANT}"]
    assert result.evidence[-1] == dispute.evidence[-1]
    assert "1" in dispute.evidence[-1].summary
    assert "valid" in dispute.evidence[-1].summary
    assert "1.0" in dispute.evidence[-1].summary
    assert "valid" in dispute.summary
    assert [call.name for call in dispute.tool_calls] == ["dispute_tickets"]
    assert _finding_payload(result) == {
        "status": "finding",
        "judge_status": "judged",
        "judge_reason": None,
        "judge_disputes": 1,
        "judge_verdict": "valid",
        "judge_score": 1.0,
        "judge_passed": True,
        "judge_rationale": "1 dispute workflow(s) pass the rubric",
    }


def test_invalid_record_creates_a_finding_without_tickets_or_chargebacks(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    record = _record(invalid=True)
    source = StubDisputeRecordSource((record,))
    verdict = judge_dispute(record)
    rationale = f"dispute {DISPUTE_ID}: " + "; ".join(reason.detail for reason in verdict.reasons)

    # Act
    with session_factory.begin() as session:
        result = run_agents(session, SCOPE, QUESTION, NOW, dispute_records=source)

    # Assert
    dispute = _dispute(result)
    assert source.calls == [(SCOPE, MAX_JUDGED_DISPUTES)]
    assert dispute.status == "finding"
    assert dispute.judge == DisputeJudgement(
        status="judged",
        reason=None,
        disputes=1,
        verdict="invalid",
        score=0.9,
        passed=False,
        rationale=rationale[:300],
        dispute_ids=(DISPUTE_ID,),
    )
    assert [item.ref for item in dispute.evidence] == [f"dispute_judge:{MERCHANT}"]
    assert [call.name for call in dispute.tool_calls] == ["dispute_tickets"]
    assert _finding_payload(result) == {
        "status": "finding",
        "judge_status": "judged",
        "judge_reason": None,
        "judge_disputes": 1,
        "judge_verdict": "invalid",
        "judge_score": 0.9,
        "judge_passed": False,
        "judge_rationale": rationale[:300],
    }


def test_foreign_merchant_record_is_skipped_without_evidence_or_status_changes(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    source = StubDisputeRecordSource((_record(merchant_id=OTHER_MERCHANT),))

    # Act
    with session_factory.begin() as session:
        result = run_agents(session, SCOPE, QUESTION, NOW, dispute_records=source)

    # Assert
    dispute = _dispute(result)
    assert source.calls == [(SCOPE, MAX_JUDGED_DISPUTES)]
    assert dispute.judge == DisputeJudgement(
        status="skipped",
        reason="no dispute workflow record for this merchant",
        disputes=0,
        verdict=None,
        score=None,
        passed=None,
        rationale=None,
        dispute_ids=(),
    )
    assert dispute.status == "no_finding"
    assert dispute.summary == "No dispute ticket and no chargeback is on record."
    assert dispute.evidence == ()
    assert result.evidence == ()
    assert [call.name for call in dispute.tool_calls] == ["dispute_tickets"]
    assert _finding_payload(result) == {
        "status": "no_finding",
        "judge_status": "skipped",
        "judge_reason": "no dispute workflow record for this merchant",
        "judge_disputes": 0,
        "judge_verdict": None,
        "judge_score": None,
        "judge_passed": None,
        "judge_rationale": None,
    }


def test_only_the_dispute_contribution_carries_the_judgement_when_all_agents_run(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    source = StubDisputeRecordSource((_record(),))

    # Act
    with session_factory.begin() as session:
        result = run_agents(
            session,
            SCOPE,
            "Is it fraud, why is my payout short, and is there a dispute?",
            NOW,
            dispute_records=source,
        )

    # Assert
    assert result.plan == ("fraud", "settlement", "dispute")
    assert {contribution.agent: contribution.judge for contribution in result.contributions} == {
        "fraud": None,
        "settlement": None,
        "dispute": DisputeJudgement(
            status="judged",
            reason=None,
            disputes=1,
            verdict="valid",
            score=1.0,
            passed=True,
            rationale="1 dispute workflow(s) pass the rubric",
            dispute_ids=(DISPUTE_ID,),
        ),
    }
    assert _dispute(result).status == "no_finding"
    assert source.calls == [(SCOPE, MAX_JUDGED_DISPUTES)]


def test_judge_wiring_keeps_the_dispute_tool_allowlist_unchanged() -> None:
    # Arrange
    expected_tools = frozenset({"dispute_tickets", "explain_settlement"})

    # Act
    actual_tools = AGENT_TOOLS["dispute"]

    # Assert
    assert actual_tools == expected_tools
