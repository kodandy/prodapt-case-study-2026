import re
from typing import Literal

TicketSource = Literal["card_testing", "assistant_escalation", "dispute", "unknown_payment", "kyc_review"]
EscalationTopic = Literal["fraud", "settlement", "compliance", "payments"]
Queue = Literal["fraud", "payments_ops", "settlement", "compliance"]
Priority = Literal["high", "normal", "low"]

_ROUTES: dict[TicketSource, tuple[Queue, Priority]] = {
    "card_testing": ("fraud", "high"),
    "dispute": ("compliance", "high"),
    "unknown_payment": ("payments_ops", "normal"),
    "kyc_review": ("compliance", "normal"),
}
_ESCALATION_ROUTES: dict[EscalationTopic, tuple[Queue, Priority]] = {
    "fraud": ("fraud", "normal"),
    "settlement": ("settlement", "normal"),
    "compliance": ("compliance", "normal"),
    "payments": ("payments_ops", "normal"),
}


def _words(*phrases: str) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(p) for p in phrases) + r")\b", re.IGNORECASE)


# Order is precedence: the first topic whose words appear wins.
_TOPIC_RULES: tuple[tuple[EscalationTopic, re.Pattern[str]], ...] = (
    ("fraud", _words("fraud", "card testing", "stolen", "suspicious")),
    (
        "compliance",
        _words(
            "dispute",
            "disputes",
            "chargeback",
            "chargebacks",
            "kyc",
            "aml",
            "sanction",
            "sanctions",
            "compliance",
        ),
    ),
    ("settlement", _words("settlement", "payout", "payouts", "statement", "fee", "fees", "reserve")),
)


def route_ticket(source: TicketSource, topic: EscalationTopic | None = None) -> tuple[Queue, Priority]:
    """Deterministic routing table: the queue and priority a ticket of this source starts in.

    `topic` only steers assistant escalations; the other sources have one fixed route.
    """
    if source == "assistant_escalation":
        return _ESCALATION_ROUTES[topic or "payments"]
    return _ROUTES[source]


def classify_escalation_topic(question: str) -> EscalationTopic:
    """Keyword rules (whole words, case-insensitive); fraud > compliance > settlement, otherwise payments."""
    for topic, pattern in _TOPIC_RULES:
        if pattern.search(question):
            return topic
    return "payments"
