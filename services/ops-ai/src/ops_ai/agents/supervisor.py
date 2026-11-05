"""Merchant investigation by a supervisor and the fraud, settlement and dispute agents (R2-1, R2-6).

The supervisor plans which agents to ask, then delivers messages FIFO until the queue is empty or the message
budget is spent. Each agent calls only its allowlisted tools; the combined finding cites only what they found.
"""

import dataclasses
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ops_ai.agents.bus import InProcessBus, MessageBudgetExceededError, MessageTransport, persist_messages
from ops_ai.agents.messages import (
    AgentContribution,
    AgentMessage,
    AgentName,
    ContributionStatus,
    JsonScalar,
    MessageIntent,
)
from ops_ai.agents.tools import AgentToolbox, DisputeTickets
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.feedback.thresholds import is_flagged
from ops_ai.investigation.service import ToolCall
from ops_ai.judge.dispute_summary import DisputeJudgement, DisputeRecordSource, judge_merchant_disputes
from ops_ai.scope import Scope
from ops_ai.settlement.statement import SettlementStatement

FRAUD_WINDOW = timedelta(hours=24)
PLAN_ORDER: tuple[AgentName, ...] = ("fraud", "settlement", "dispute")
PLAN_KEYWORDS: dict[AgentName, tuple[str, ...]] = {
    "fraud": ("fraud", "card testing", "card-testing", "micro", "suspicious"),
    "settlement": ("payout", "settlement", "fee", "deduct"),
    "dispute": ("dispute", "chargeback"),
}


@dataclass(frozen=True)
class AgentInvestigation:
    correlation_id: uuid.UUID
    plan: tuple[AgentName, ...]
    contributions: tuple[AgentContribution, ...]
    messages: tuple[AgentMessage, ...]
    finding: str
    evidence: tuple[EvidenceItem, ...]
    tool_calls: tuple[ToolCall, ...]
    truncated: bool
    mode: AnswerMode
    text: str | None
    model: str | None


def plan_agents(question: str) -> tuple[AgentName, ...]:
    lowered = question.lower()
    plan = tuple(agent for agent in PLAN_ORDER if any(word in lowered for word in PLAN_KEYWORDS[agent]))
    return plan or PLAN_ORDER


@dataclass
class AgentState:
    """What one agent has done so far in an investigation."""

    toolbox: AgentToolbox
    handled: bool = False
    status: ContributionStatus = "no_finding"
    summaries: list[str] = field(default_factory=list)
    evidence: list[EvidenceItem] = field(default_factory=list)
    judge: DisputeJudgement | None = None

    def contribution(self) -> AgentContribution:
        return AgentContribution(
            agent=self.toolbox.agent,
            status=self.status,
            summary=" ".join(self.summaries),
            evidence=tuple(self.evidence),
            tool_calls=tuple(self.toolbox.tool_calls),
            judge=self.judge,
        )


def combine_evidence(contributions: Sequence[AgentContribution]) -> tuple[EvidenceItem, ...]:
    seen: set[str] = set()
    combined: list[EvidenceItem] = []
    for contribution in contributions:
        for item in contribution.evidence:
            if item.ref not in seen:
                seen.add(item.ref)
                combined.append(item)
    return tuple(combined)


def _money(minor: int, currency: str) -> str:
    sign = "-" if minor < 0 else ""
    units, cents = divmod(abs(minor), 100)
    return f"{sign}{units}.{cents:02d} {currency}"


def _line_total(statement: SettlementStatement, kind: str) -> int:
    return sum(line.amount_minor for line in statement.lines if line.kind == kind)


class MerchantRun:
    """One merchant investigation: the agents' handlers and their state."""

    def __init__(
        self,
        session: Session,
        scope: Scope,
        now: datetime,
        bus: InProcessBus,
        dispute_records: DisputeRecordSource | None = None,
    ) -> None:
        self.scope = scope
        self.dispute_records = dispute_records
        self.now = now
        self.bus = bus
        self.states: dict[AgentName, AgentState] = {
            agent: AgentState(AgentToolbox(agent, session)) for agent in PLAN_ORDER
        }
        self._statement: SettlementStatement | None = None
        self._statement_read = False
        self._dispute_tickets: DisputeTickets | None = None
        self._handlers: dict[tuple[AgentName, MessageIntent], Callable[[AgentMessage], None]] = {
            ("fraud", "investigate"): self._fraud_investigate,
            ("settlement", "investigate"): self._settlement_investigate,
            ("settlement", "chargeback_impact"): self._settlement_chargeback_impact,
            ("dispute", "investigate"): self._dispute_start,
            ("dispute", "check_disputes"): self._dispute_start,
            ("dispute", "chargeback_impact_result"): self._dispute_conclude,
        }

    def send(
        self,
        sender: AgentName,
        recipient: AgentName,
        intent: MessageIntent,
        payload: dict[str, JsonScalar],
        evidence_refs: Sequence[str] = (),
    ) -> None:
        self.bus.send(sender, recipient, intent, payload, evidence_refs)

    def run(self, plan: Sequence[AgentName]) -> bool:
        """Delivers until the queue is empty; True when the message budget cut the conversation short."""
        try:
            for agent in plan:
                self.send("supervisor", agent, "investigate", {})
            while (message := self.bus.next_message()) is not None:
                handler = self._handlers.get((message.recipient, message.intent))
                if handler is not None:
                    handler(message)
        except MessageBudgetExceededError:
            return True
        return False

    def contributions(self) -> tuple[AgentContribution, ...]:
        return tuple(self.states[agent].contribution() for agent in PLAN_ORDER if self.states[agent].handled)

    # --- fraud ---------------------------------------------------------------------------------------------

    def _fraud_investigate(self, _message: AgentMessage) -> None:
        state = self.states["fraud"]
        state.handled = True
        analysis = state.toolbox.analyze_fraud(self.scope, self.now - FRAUD_WINDOW, self.now)
        rule = state.toolbox.fraud_rule_version(self.scope)
        flagged = [episode for episode in analysis.episodes if is_flagged(episode, rule)]
        criteria = (
            f"fraud rule v{rule.version} (at least {rule.min_micro_payments} micro payments, "
            f"{rule.min_micro_share:.0%} micro share, {rule.min_distinct_cards} distinct cards)"
        )
        if not flagged:
            state.summaries.append(f"No card-testing episode in the last 24 hours meets {criteria}.")
            self.send("fraud", "supervisor", "finding", {"status": "no_finding", "episodes": 0})
            return
        state.status = "finding"
        for episode in flagged:
            cards = "unknown" if episode.distinct_cards is None else str(episode.distinct_cards)
            state.evidence.append(
                EvidenceItem(
                    ref=f"episode:{episode.first_payment_id}",
                    summary=(
                        f"{episode.micro_payments} micro payments "
                        f"({episode.micro_share:.0%} of card payments) "
                        f"on {cards} cards from {episode.start.isoformat()} to {episode.end.isoformat()}, "
                        f"{episode.declined} declined"
                    ),
                )
            )
        state.summaries.append(
            f"{len(flagged)} card-testing episode(s) in the last 24 hours meet {criteria}."
        )
        refs = [item.ref for item in state.evidence]
        self.send("fraud", "dispute", "check_disputes", {"episodes": len(flagged)}, refs)
        self.send("fraud", "supervisor", "finding", {"status": "finding", "episodes": len(flagged)}, refs)

    # --- settlement ----------------------------------------------------------------------------------------

    def _statement_once(self, state: AgentState) -> SettlementStatement | None:
        if not self._statement_read:
            self._statement = state.toolbox.explain_settlement(self.scope, None)
            self._statement_read = True
        return self._statement

    def _statement_evidence(self, statement: SettlementStatement) -> EvidenceItem:
        return EvidenceItem(
            ref=f"statement:{statement.period}",
            summary=(
                f"Statement {statement.period}: gross {_money(statement.gross_minor, statement.currency)}, "
                f"net {_money(statement.net_minor, statement.currency)}, gap to gross minus MDR "
                f"{_money(statement.gap_minor, statement.currency)}, verified {statement.verified}"
            ),
        )

    def _settlement_investigate(self, _message: AgentMessage) -> None:
        state = self.states["settlement"]
        state.handled = True
        statement = self._statement_once(state)
        if statement is None:
            state.summaries.append("No settlement statement is on record.")
        elif statement.gap_minor != 0:
            state.status = "finding"
            state.evidence.append(self._statement_evidence(statement))
            state.summaries.append(
                f"The {statement.period} payout is {_money(statement.gap_minor, statement.currency)} below "
                "gross minus MDR because of fixed fees, FX, refunds, chargebacks and the reserve."
            )
        else:
            state.summaries.append(f"The {statement.period} payout matches gross minus MDR.")
        self.send(
            "settlement",
            "supervisor",
            "finding",
            {"status": state.status},
            [item.ref for item in state.evidence],
        )

    def _settlement_chargeback_impact(self, _message: AgentMessage) -> None:
        state = self.states["settlement"]
        first_contact = not state.handled
        state.handled = True
        statement = self._statement_once(state)
        if statement is None:
            chargebacks, fees, period = 0, 0, None
        else:
            chargebacks = _line_total(statement, "chargebacks")
            fees = _line_total(statement, "chargeback_fees")
            period = statement.period
        if first_contact:
            if statement is not None and (chargebacks != 0 or fees != 0):
                state.status = "finding"
                state.evidence.append(self._statement_evidence(statement))
                state.summaries.append(
                    f"Chargebacks of {_money(chargebacks, statement.currency)} and chargeback fees of "
                    f"{_money(fees, statement.currency)} were deducted in {statement.period}."
                )
            else:
                state.summaries.append("No chargeback was deducted in the latest statement.")
        self.send(
            "settlement",
            "dispute",
            "chargeback_impact_result",
            {"chargebacks_minor": chargebacks, "chargeback_fees_minor": fees, "period": period},
            [] if statement is None else [f"statement:{statement.period}"],
        )

    # --- dispute -------------------------------------------------------------------------------------------

    def _dispute_start(self, _message: AgentMessage) -> None:
        state = self.states["dispute"]
        if state.handled:
            return
        state.handled = True
        self._dispute_tickets = state.toolbox.dispute_tickets(self.scope)
        self.send(
            "dispute", "settlement", "chargeback_impact", {"dispute_tickets": self._dispute_tickets.total}
        )

    def _dispute_conclude(self, message: AgentMessage) -> None:
        state = self.states["dispute"]
        tickets = self._dispute_tickets or DisputeTickets(open=0, assigned=0, resolved=0)
        chargebacks = message.payload.get("chargebacks_minor")
        fees = message.payload.get("chargeback_fees_minor")
        period = message.payload.get("period")
        has_chargebacks = isinstance(chargebacks, int) and chargebacks != 0
        if tickets.total > 0 or has_chargebacks:
            state.status = "finding"
            state.evidence.append(
                EvidenceItem(
                    ref=f"disputes:{self.scope.merchant_id}",
                    summary=(
                        f"{tickets.total} dispute ticket(s) "
                        f"({tickets.open} open, {tickets.assigned} assigned, "
                        f"{tickets.resolved} resolved); chargebacks {chargebacks} and chargeback fees {fees} "
                        f"(minor units) in statement {period or 'none'}"
                    ),
                )
            )
            state.summaries.append(
                f"{tickets.total} dispute ticket(s) are on record and the latest statement carries "
                f"{chargebacks} in chargebacks (minor units)."
            )
        else:
            state.summaries.append("No dispute ticket and no chargeback is on record.")
        judgement = judge_merchant_disputes(self.scope, self.dispute_records)
        state.judge = judgement
        if judgement.status == "judged":
            state.evidence.append(
                EvidenceItem(
                    ref=f"dispute_judge:{self.scope.merchant_id}",
                    summary=(
                        f"Dispute judge: {judgement.disputes} dispute workflow(s), worst verdict "
                        f"{judgement.verdict}, rubric score {judgement.score}"
                    ),
                )
            )
            state.summaries.append(
                f"The dispute judge rates {judgement.disputes} dispute workflow(s) {judgement.verdict}."
            )
            if judgement.passed is False:
                state.status = "finding"
        self.send(
            "dispute",
            "supervisor",
            "finding",
            {
                "status": state.status,
                "judge_status": judgement.status,
                "judge_reason": judgement.reason,
                "judge_disputes": judgement.disputes,
                "judge_verdict": judgement.verdict,
                "judge_score": judgement.score,
                "judge_passed": judgement.passed,
                "judge_rationale": judgement.rationale,
            },
            [item.ref for item in state.evidence],
        )


def run_agents(
    session: Session,
    scope: Scope,
    question: str,
    now: datetime,
    transport: MessageTransport | None = None,
    dispute_records: DisputeRecordSource | None = None,
) -> AgentInvestigation:
    """Runs the planned agents and persists the message trail (the caller commits); no LLM call, so the
    caller can release the DB connection before `narrate_investigation`."""
    plan = plan_agents(question)
    bus = InProcessBus(uuid.uuid4(), lambda: now, transport=transport)
    run = MerchantRun(session, scope, now, bus, dispute_records)
    truncated = run.run(plan)
    contributions = run.contributions()
    evidence = combine_evidence(contributions)
    messages = bus.history
    persist_messages(session, messages, scope.merchant_id, None)
    return AgentInvestigation(
        correlation_id=bus.correlation_id,
        plan=plan,
        contributions=contributions,
        messages=messages,
        finding="\n".join(f"{c.agent}: {c.summary}" for c in contributions),
        evidence=evidence,
        tool_calls=tuple(call for c in contributions for call in c.tool_calls),
        truncated=truncated,
        mode="t2_fallback",
        text=None,
        model=None,
    )


def narrate_investigation(result: AgentInvestigation, question: str, llm: LlmPort) -> AgentInvestigation:
    """The LLM narrates the combined evidence; LlmUnavailableError -> t2_fallback with the same findings."""
    try:
        response = llm.complete(LlmRequest(question=question[:MAX_QUESTION_CHARS], evidence=result.evidence))
    except LlmUnavailableError:
        return result
    return dataclasses.replace(result, mode="llm", text=response.text, model=response.model)


def investigate_with_agents(
    session: Session,
    scope: Scope,
    question: str,
    now: datetime,
    llm: LlmPort,
    transport: MessageTransport | None = None,
) -> AgentInvestigation:
    """run_agents then narrate_investigation on the same session (the caller commits)."""
    return narrate_investigation(run_agents(session, scope, question, now, transport), question, llm)
