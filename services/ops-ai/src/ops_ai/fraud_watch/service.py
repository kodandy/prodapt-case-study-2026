import dataclasses
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.fraud_watch.analysis import CardTestingEpisode, FraudAnalysis, analyze_fraud
from ops_ai.fraud_watch.models import FraudAlert
from ops_ai.fraud_watch.watch import describe_cards, format_span
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import Scope
from ops_ai.tickets.models import Ticket

FraudIntent = Literal["card_testing", "unsupported"]
FraudCause = Literal["card_testing", "no_card_testing", "unsupported"]

WINDOW = timedelta(hours=24)
PAYMENT_EVIDENCE_LIMIT = 5
INTENT_KEYWORDS = ("card testing", "card-testing", "micro")
CARD_TESTING_FIXES = (
    "Add a velocity limit on card payments of 1.00 or less, per card and per client IP.",
    "Require 3-D Secure for card payments of 1.00 or less until the burst stops.",
    "Do not refund or contact the cards in the burst from this channel; the fraud team works the ticket.",
)


@dataclass(frozen=True)
class AlertRef:
    alert_id: uuid.UUID
    ticket_id: uuid.UUID | None
    queue: str | None
    first_payment_id: uuid.UUID


@dataclass(frozen=True)
class FraudInvestigation:
    intent: FraudIntent
    cause_label: FraudCause
    analysis: FraudAnalysis | None
    alerts: tuple[AlertRef, ...]
    suggested_fixes: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    evidence: tuple[EvidenceItem, ...]
    mode: AnswerMode
    text: str | None
    """None in t2_fallback."""
    model: str | None


UNSUPPORTED_FRAUD = FraudInvestigation(
    intent="unsupported",
    cause_label="unsupported",
    analysis=None,
    alerts=(),
    suggested_fixes=(),
    tool_calls=(),
    evidence=(),
    mode="t2_fallback",
    text=None,
    model=None,
)


def classify_fraud_intent(question: str) -> FraudIntent:
    lowered = question.lower()
    return "card_testing" if any(keyword in lowered for keyword in INTENT_KEYWORDS) else "unsupported"


def investigate_fraud(
    session: Session, scope: Scope, question: str, now: datetime, llm: LlmPort
) -> FraudInvestigation:
    """Tools first: analyze_fraud over the 24 hours before now, then the alerts of the flagged episodes.

    The label, ids and numbers come from the tools; the LLM only narrates the evidence
    (LlmUnavailableError → t2_fallback with the same fields). Unsupported questions call no tool and no LLM.
    """
    bounded = question[:MAX_QUESTION_CHARS]
    if classify_fraud_intent(bounded) == "unsupported":
        return UNSUPPORTED_FRAUD
    start = now - WINDOW
    analysis = analyze_fraud(session, scope, start, now)
    tool_calls = (ToolCall("analyze_fraud", {"start": start.isoformat(), "end": now.isoformat()}),)
    flagged = [episode for episode in analysis.episodes if episode.flagged]
    alerts = _alerts(session, scope, flagged)
    evidence = (
        *(_episode_evidence(episode) for episode in flagged),
        *(_alert_evidence(alert) for alert in alerts),
        *(_payment_evidence(session, scope, flagged[0]) if flagged else []),
    )
    result = FraudInvestigation(
        intent="card_testing",
        cause_label="card_testing" if flagged else "no_card_testing",
        analysis=analysis,
        alerts=alerts,
        suggested_fixes=CARD_TESTING_FIXES if flagged else (),
        tool_calls=tool_calls,
        evidence=evidence,
        mode="t2_fallback",
        text=None,
        model=None,
    )
    session.close()
    try:
        response = llm.complete(LlmRequest(question=bounded, evidence=evidence))
    except LlmUnavailableError:
        return result
    return dataclasses.replace(result, mode="llm", text=response.text, model=response.model)


def _alerts(session: Session, scope: Scope, flagged: list[CardTestingEpisode]) -> tuple[AlertRef, ...]:
    if not flagged:
        return ()
    rows = session.execute(
        select(FraudAlert.alert_id, FraudAlert.first_payment_id, Ticket.ticket_id, Ticket.queue)
        .outerjoin(Ticket, Ticket.alert_id == FraudAlert.alert_id)
        .where(
            FraudAlert.merchant_id == scope.merchant_id,
            FraudAlert.first_payment_id.in_([episode.first_payment_id for episode in flagged]),
        )
        .order_by(FraudAlert.created_at, FraudAlert.alert_id)
    ).all()
    order = {episode.first_payment_id: index for index, episode in enumerate(flagged)}
    return tuple(
        AlertRef(
            alert_id=row.alert_id,
            ticket_id=row.ticket_id,
            queue=row.queue,
            first_payment_id=row.first_payment_id,
        )
        for row in sorted(rows, key=lambda row: order[row.first_payment_id])
    )


def _percent(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _episode_evidence(episode: CardTestingEpisode) -> EvidenceItem:
    bins = ", ".join(episode.card_bins) if episode.card_bins else "none"
    return EvidenceItem(
        ref=f"episode:{episode.first_payment_id}",
        summary=(
            f"{episode.micro_payments} micro card payments over {format_span(episode)} "
            f"({episode.start.isoformat()} to {episode.end.isoformat()}), "
            f"{describe_cards(episode.distinct_cards)}, micro share {_percent(episode.micro_share)}, "
            f"decline ratio {_percent(episode.decline_ratio)}, BINs {bins}"
        ),
    )


def _alert_evidence(alert: AlertRef) -> EvidenceItem:
    ticket = f"ticket {alert.ticket_id} in the {alert.queue} queue" if alert.ticket_id else "no ticket"
    return EvidenceItem(
        ref=f"alert:{alert.alert_id}",
        summary=f"Card-testing alert for the episode starting at payment {alert.first_payment_id}: {ticket}",
    )


def _payment_evidence(session: Session, scope: Scope, episode: CardTestingEpisode) -> list[EvidenceItem]:
    """The newest micro payments of the episode, newest first."""
    newest = list(reversed(episode.payment_ids))[:PAYMENT_EVIDENCE_LIMIT]
    rows = {
        row.payment_id: row
        for row in session.execute(
            select(
                PaymentFact.payment_id,
                PaymentFact.amount_minor,
                PaymentFact.currency,
                PaymentFact.state,
                PaymentFact.card_bin,
                PaymentFact.occurred_at,
            ).where(PaymentFact.merchant_id == scope.merchant_id, PaymentFact.payment_id.in_(newest))
        )
    }
    return [
        EvidenceItem(
            ref=f"payment:{payment_id}",
            summary=(
                f"Micro card payment {rows[payment_id].state}: {rows[payment_id].amount_minor} "
                f"{rows[payment_id].currency} (minor units) on BIN {rows[payment_id].card_bin or 'none'} "
                f"at {rows[payment_id].occurred_at.isoformat()}"
            ),
        )
        for payment_id in newest
        if payment_id in rows
    ]
