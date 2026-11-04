import hashlib
import re
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.clock import Clock
from ops_ai.scope import Scope
from ops_ai.tickets.models import Ticket
from ops_ai.tickets.routing import classify_escalation_topic
from ops_ai.tickets.service import NewTicket, TicketOpened, open_ticket

EscalationMode = Literal["llm", "t2_fallback"]
ESCALATION_TITLE = "Assistant escalation"
REASON_QUESTION_CHARS = 300
ESCALATION_DAILY_CAP = 50
"""Per merchant per UTC day: bounds ticket creation from /ask (a nonce in the question defeats the dedupe)."""

# The question lands in ops-visible storage: card numbers, emails and bearer tokens are masked first.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)"), "[card-number]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[token]"),
)

_HUMAN_REQUEST = re.compile(
    r"\b(?:human|real person|a person|agent|someone|escalate|support team|operator|representative)\b",
    re.IGNORECASE,
)


def wants_human(question: str) -> bool:
    return _HUMAN_REQUEST.search(question) is not None


def redact_question(text: str) -> str:
    for pattern, mask in _REDACTIONS:
        text = pattern.sub(mask, text)
    return "".join(char for char in text if char.isprintable())


def escalate_assistant_turn(
    session: Session, clock: Clock, scope: Scope, question: str, mode: EscalationMode
) -> TicketOpened | None:
    """Opens a ticket when the assistant abstained (t2_fallback) or the merchant asked for a human.

    Dedupe key: merchant + UTC day + hash of the normalized question, so a retried question is one ticket.
    Does not commit.
    """
    human = wants_human(question)
    if not human and mode != "t2_fallback":
        return None
    collapsed = " ".join(question.split())
    shown = redact_question(collapsed)[:REASON_QUESTION_CHARS]
    reason = (
        f"Merchant asked for a human: {shown}"
        if human
        else f"Assistant could not answer (LLM unavailable): {shown}"
    )
    digest = hashlib.sha256(collapsed.lower().encode()).hexdigest()
    day = clock().date().isoformat()
    if _opened_today(session, clock, scope) >= ESCALATION_DAILY_CAP:
        return None
    return open_ticket(
        session,
        clock,
        NewTicket(
            source="assistant_escalation",
            source_key=f"assistant_escalation:{scope.merchant_id}:{day}:{digest}",
            merchant_id=scope.merchant_id,
            title=ESCALATION_TITLE,
            reason=reason,
            topic=classify_escalation_topic(question),
        ),
    )


def _opened_today(session: Session, clock: Clock, scope: Scope) -> int:
    day_start = clock().replace(hour=0, minute=0, second=0, microsecond=0)
    count = session.scalar(
        select(func.count())
        .select_from(Ticket)
        .where(
            Ticket.merchant_id == scope.merchant_id,
            Ticket.source == "assistant_escalation",
            Ticket.created_at >= day_start,
        )
    )
    return count or 0
