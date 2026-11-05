"""Collaborative root-cause analysis of a sender's transfer by the fraud, compliance and settlement agents
(R2-6, SQ2).

The agents only ever exchange sender-safe facts: no rule id, no reason code, and a fraud hold reads exactly
like a pending transfer (`fraud_hold` is always False towards the sender).
"""

import dataclasses
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from ops_ai.agents.bus import InProcessBus, MessageBudgetExceededError, MessageTransport, persist_messages
from ops_ai.agents.kyc import KycProfileView
from ops_ai.agents.messages import AgentContribution, AgentMessage, AgentName, MessageIntent
from ops_ai.agents.supervisor import AgentState, combine_evidence
from ops_ai.agents.tools import AgentToolbox
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.scope import UserScope
from ops_ai.transfers.service import _evidence, _suggested_fixes
from ops_ai.transfers.tools import CauseCode, RemediationOwner, TransferStatus, classify_transfer

RCA_AGENTS: tuple[AgentName, ...] = ("fraud", "compliance", "settlement")
SENDER_ACTIONABLE: frozenset[CauseCode] = frozenset(
    {"cross_border_limit_for_kyc_tier", "insufficient_funds", "kyc_verification_failed"}
)


@dataclass(frozen=True)
class TransferRca:
    correlation_id: uuid.UUID
    cause_code: CauseCode
    remediation_owner: RemediationOwner
    contributions: tuple[AgentContribution, ...]
    messages: tuple[AgentMessage, ...]
    suggested_fixes: tuple[str, ...]
    evidence: tuple[EvidenceItem, ...]
    truncated: bool
    mode: AnswerMode
    text: str | None
    model: str | None


def _item(items: tuple[EvidenceItem, ...], prefix: str) -> list[EvidenceItem]:
    return [item for item in items if item.ref.startswith(prefix)]


class TransferRun:
    """One transfer RCA: each agent re-reads the transfer through its own allowlisted tools."""

    def __init__(
        self, session: Session, scope: UserScope, transfer: TransferStatus, bus: InProcessBus
    ) -> None:
        self.scope = scope
        self.transfer_id = transfer.transfer_id
        self.bus = bus
        self.states: dict[AgentName, AgentState] = {
            agent: AgentState(AgentToolbox(agent, session)) for agent in RCA_AGENTS
        }
        self._fraud_view = transfer
        self._compliance_view: TransferStatus | None = None
        self._kyc: KycProfileView | None = None
        self._handlers: dict[tuple[AgentName, MessageIntent], Callable[[AgentMessage], None]] = {
            ("fraud", "assess_transfer"): self._fraud_assess,
            ("compliance", "check_kyc"): self._compliance_check_kyc,
            ("settlement", "verify_funds"): self._settlement_verify_funds,
            ("compliance", "funds_verified"): self._compliance_conclude,
        }

    def run(self) -> bool:
        """True when the message budget cut the conversation short."""
        try:
            self.bus.send("supervisor", "fraud", "assess_transfer", {"transfer_id": str(self.transfer_id)})
            while (message := self.bus.next_message()) is not None:
                handler = self._handlers.get((message.recipient, message.intent))
                if handler is not None:
                    handler(message)
        except MessageBudgetExceededError:
            return True
        return False

    def contributions(self) -> tuple[AgentContribution, ...]:
        return tuple(self.states[agent].contribution() for agent in RCA_AGENTS if self.states[agent].handled)

    def _fraud_assess(self, _message: AgentMessage) -> None:
        state = self.states["fraud"]
        state.handled = True
        transfer = self._fraud_view
        cause, _owner = classify_transfer(transfer)
        state.evidence.extend(_item(_evidence(transfer, cause), "transfer:"))
        state.summaries.append("Nothing on the fraud side for the sender to act on.")
        refs = [item.ref for item in state.evidence]
        if transfer.status == "failed":
            self.bus.send(
                "fraud",
                "compliance",
                "check_kyc",
                {"transfer_id": str(transfer.transfer_id), "fraud_hold": False},
                refs,
            )
        else:
            state.summaries.append("The transfer was not declined.")
        self.bus.send("fraud", "supervisor", "finding", {"fraud_hold": False}, refs)

    def _compliance_check_kyc(self, _message: AgentMessage) -> None:
        state = self.states["compliance"]
        state.handled = True
        self._compliance_view = state.toolbox.transfer_status(self.scope, self.transfer_id)
        self._kyc = state.toolbox.kyc_profile(self.scope)
        if self._compliance_view is None:
            return
        self.bus.send(
            "compliance",
            "settlement",
            "verify_funds",
            {"transfer_id": str(self._compliance_view.transfer_id)},
        )

    def _settlement_verify_funds(self, _message: AgentMessage) -> None:
        state = self.states["settlement"]
        state.handled = True
        transfer = state.toolbox.transfer_status(self.scope, self.transfer_id)
        if transfer is None:
            return
        balance = transfer.sender_balance_minor
        covered = balance is not None and balance >= transfer.amount_minor
        cause, _owner = classify_transfer(transfer)
        state.status = "finding"
        state.evidence.extend(_item(_evidence(transfer, cause), "balance:"))
        if balance is None:
            state.summaries.append("The balance at decision time is not on record.")
        elif covered:
            state.summaries.append("The balance at decision time covered the transfer amount.")
        else:
            state.summaries.append("The balance at decision time did not cover the transfer amount.")
        self.bus.send(
            "settlement",
            "compliance",
            "funds_verified",
            {"balance_covered_amount": covered},
            [item.ref for item in state.evidence],
        )

    def _compliance_conclude(self, _message: AgentMessage) -> None:
        state = self.states["compliance"]
        transfer = self._compliance_view
        if transfer is None:
            return
        cause, owner = classify_transfer(transfer)
        kyc_status = None if self._kyc is None else self._kyc.kyc_status
        cross_border = transfer.country_sender != transfer.country_receiver
        state.evidence.extend(_item(_evidence(transfer, cause), "limit:"))
        if self._kyc is not None:
            state.evidence.append(
                EvidenceItem(
                    ref=f"kyc:{self._kyc.user_id}",
                    summary=f"KYC status {self._kyc.kyc_status}, country {self._kyc.country}",
                )
            )
        if cause in SENDER_ACTIONABLE:
            state.status = "finding"
        tier = "unknown" if transfer.kyc_tier is None else str(transfer.kyc_tier)
        state.summaries.append(
            f"Cause {cause}, to be fixed by the {owner}; "
            f"KYC status {kyc_status or 'unknown'}, KYC tier {tier}, "
            f"{'cross-border' if cross_border else 'domestic'} transfer."
        )
        self.bus.send(
            "compliance",
            "supervisor",
            "root_cause",
            {
                "cause_code": cause,
                "remediation_owner": owner,
                "kyc_status": kyc_status,
                "kyc_tier": transfer.kyc_tier,
                "cross_border": cross_border,
            },
            [item.ref for item in state.evidence],
        )


def run_transfer_rca(
    session: Session,
    scope: UserScope,
    transfer_id: uuid.UUID | None,
    now: datetime,
    transport: MessageTransport | None = None,
) -> TransferRca | None:
    """The sender's own transfer (the newest failed one without an id) investigated by the agents; None when
    the scope has no such transfer (no message). The trail is persisted with the user id only; the caller
    commits. No LLM call, so the caller can release the DB connection before `narrate_transfer_rca`."""
    intake = AgentToolbox("fraud", session)
    transfer = intake.transfer_status(scope, transfer_id)
    if transfer is None:
        return None
    bus = InProcessBus(uuid.uuid4(), lambda: now, transport=transport)
    run = TransferRun(session, scope, transfer, bus)
    run.states["fraud"].toolbox.tool_calls.extend(intake.tool_calls)
    truncated = run.run()
    cause, owner = classify_transfer(transfer)
    contributions = run.contributions()
    evidence = combine_evidence(contributions)
    messages = bus.history
    persist_messages(session, messages, None, scope.user_id)
    return TransferRca(
        correlation_id=bus.correlation_id,
        cause_code=cause,
        remediation_owner=owner,
        contributions=contributions,
        messages=messages,
        suggested_fixes=_suggested_fixes(transfer, cause),
        evidence=evidence,
        truncated=truncated,
        mode="t2_fallback",
        text=None,
        model=None,
    )


def narrate_transfer_rca(result: TransferRca, question: str, llm: LlmPort) -> TransferRca:
    """The LLM narrates the combined evidence; LlmUnavailableError -> t2_fallback with the same findings."""
    try:
        response = llm.complete(LlmRequest(question=question[:MAX_QUESTION_CHARS], evidence=result.evidence))
    except LlmUnavailableError:
        return result
    return dataclasses.replace(result, mode="llm", text=response.text, model=response.model)


def investigate_transfer_rca(
    session: Session,
    scope: UserScope,
    question: str,
    transfer_id: uuid.UUID | None,
    now: datetime,
    llm: LlmPort,
    transport: MessageTransport | None = None,
) -> TransferRca | None:
    """run_transfer_rca then narrate_transfer_rca on the same session (the caller commits); None when the
    scope has no such transfer (no message, no LLM call)."""
    result = run_transfer_rca(session, scope, transfer_id, now, transport)
    return None if result is None else narrate_transfer_rca(result, question, llm)
