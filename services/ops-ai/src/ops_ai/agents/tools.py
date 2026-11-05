"""Per-agent tool allowlists over the existing read-only domain tools. A refused call raises before any query
and is not recorded."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.agents.kyc import KycProfileView, get_kyc_profile
from ops_ai.agents.messages import AgentName
from ops_ai.feedback.thresholds import RuleVersion, active_rule_version
from ops_ai.fraud_watch.analysis import FraudAnalysis, analyze_fraud
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import Scope, UserScope
from ops_ai.settlement.statement import SettlementStatement
from ops_ai.settlement.tools import explain_settlement
from ops_ai.tickets.models import Ticket
from ops_ai.transfers.tools import TransferStatus, get_payment_status

AGENT_TOOLS: Mapping[AgentName, frozenset[str]] = {
    "supervisor": frozenset(),
    "fraud": frozenset({"analyze_fraud", "fraud_rule_version", "transfer_status"}),
    "settlement": frozenset({"explain_settlement", "transfer_status"}),
    "dispute": frozenset({"dispute_tickets", "explain_settlement"}),
    "compliance": frozenset({"kyc_profile", "transfer_status"}),
}


class ToolNotAllowedError(Exception):
    def __init__(self, agent: AgentName, tool: str) -> None:
        super().__init__(f"{agent} may not call {tool}")
        self.agent: AgentName = agent
        self.tool = tool


@dataclass(frozen=True)
class DisputeTickets:
    """The merchant's dispute tickets by status."""

    open: int
    assigned: int
    resolved: int

    @property
    def total(self) -> int:
        return self.open + self.assigned + self.resolved


class AgentToolbox:
    def __init__(self, agent: AgentName, session: Session) -> None:
        self.agent: AgentName = agent
        self._session = session
        self.tool_calls: list[ToolCall] = []

    def _use(self, name: str, args: dict[str, str]) -> None:
        if name not in AGENT_TOOLS[self.agent]:
            raise ToolNotAllowedError(self.agent, name)
        self.tool_calls.append(ToolCall(name, args))

    def analyze_fraud(self, scope: Scope, start: datetime, end: datetime) -> FraudAnalysis:
        self._use("analyze_fraud", {"start": start.isoformat(), "end": end.isoformat()})
        return analyze_fraud(self._session, scope, start, end)

    def fraud_rule_version(self, scope: Scope) -> RuleVersion:
        self._use("fraud_rule_version", {})
        return active_rule_version(self._session, scope)

    def explain_settlement(self, scope: Scope, period: str | None) -> SettlementStatement | None:
        self._use("explain_settlement", {"period": "latest" if period is None else period})
        return explain_settlement(self._session, scope, period)

    def dispute_tickets(self, scope: Scope) -> DisputeTickets:
        self._use("dispute_tickets", {})
        rows = self._session.execute(
            select(Ticket.status, func.count())
            .where(Ticket.merchant_id == scope.merchant_id, Ticket.source == "dispute")
            .group_by(Ticket.status)
        ).all()
        counts = {str(status): int(count) for status, count in rows}
        return DisputeTickets(
            open=counts.get("open", 0), assigned=counts.get("assigned", 0), resolved=counts.get("resolved", 0)
        )

    def transfer_status(self, scope: UserScope, transfer_id: uuid.UUID | None) -> TransferStatus | None:
        self._use(
            "transfer_status", {"transfer_id": "latest_failed" if transfer_id is None else str(transfer_id)}
        )
        return get_payment_status(self._session, scope, transfer_id)

    def kyc_profile(self, scope: UserScope) -> KycProfileView | None:
        self._use("kyc_profile", {})
        return get_kyc_profile(self._session, scope)
