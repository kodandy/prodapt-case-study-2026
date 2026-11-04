"""Tests for route_ticket and classify_escalation_topic, written from the B12 tickets spec (section 1)."""

import pytest
from ops_ai.tickets import route_ticket
from ops_ai.tickets.routing import (
    EscalationTopic,
    Priority,
    Queue,
    TicketSource,
    classify_escalation_topic,
)
from ops_ai.tickets.routing import route_ticket as route_ticket_from_module

TOPICS: list[EscalationTopic | None] = ["fraud", "settlement", "compliance", "payments", None]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("card_testing", ("fraud", "high")),
        ("dispute", ("compliance", "high")),
        ("unknown_payment", ("payments_ops", "normal")),
    ],
    ids=["card_testing", "dispute", "unknown_payment"],
)
@pytest.mark.parametrize("topic", TOPICS, ids=[str(topic) for topic in TOPICS])
def test_route_ticket_ignores_the_topic_for_non_escalation_sources(
    source: TicketSource, topic: EscalationTopic | None, expected: tuple[Queue, Priority]
) -> None:
    # Arrange / Act
    routed = route_ticket(source, topic)

    # Assert
    assert routed == expected


@pytest.mark.parametrize(
    ("topic", "expected"),
    [
        ("fraud", ("fraud", "normal")),
        ("settlement", ("settlement", "normal")),
        ("compliance", ("compliance", "normal")),
        ("payments", ("payments_ops", "normal")),
        (None, ("payments_ops", "normal")),
    ],
    ids=["fraud", "settlement", "compliance", "payments", "none"],
)
def test_route_ticket_routes_an_assistant_escalation_by_its_topic(
    topic: EscalationTopic | None, expected: tuple[Queue, Priority]
) -> None:
    # Arrange
    source: TicketSource = "assistant_escalation"

    # Act
    routed = route_ticket(source, topic)

    # Assert
    assert routed == expected


def test_route_ticket_without_a_topic_argument_defaults_to_no_topic() -> None:
    # Arrange / Act
    routed = route_ticket("assistant_escalation")

    # Assert
    assert routed == ("payments_ops", "normal")


def test_route_ticket_is_re_exported_from_the_package() -> None:
    # Arrange / Act / Assert
    assert route_ticket is route_ticket_from_module


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Why was my payout short?", "settlement"),
        ("a chargeback on a suspicious order", "fraud"),
        ("customer opened a dispute", "compliance"),
        ("why are payments declining", "payments"),
        ("feedback", "payments"),
        ("FRAUD", "fraud"),
        ("this looks like card testing", "fraud"),
        ("Card Testing on my terminal", "fraud"),
        ("my card was stolen", "fraud"),
        ("many disputes this week", "compliance"),
        ("how many chargebacks", "compliance"),
        ("is my KYC complete", "compliance"),
        ("AML review", "compliance"),
        ("is this on a sanction list", "compliance"),
        ("sanctions screening", "compliance"),
        ("compliance report", "compliance"),
        ("where is my settlement", "settlement"),
        ("payouts are late", "settlement"),
        ("send me the statement", "settlement"),
        ("what is the fee", "settlement"),
        ("what are the fees", "settlement"),
        ("rolling reserve", "settlement"),
        ("a dispute about my payout", "compliance"),
        ("a suspicious payout", "fraud"),
        ("a chargeback fee", "compliance"),
        ("the disputed payment", "payments"),
        ("we need feed data", "payments"),
        ("", "payments"),
    ],
    ids=[
        "payout_short",
        "chargeback_suspicious_is_fraud",
        "dispute",
        "declining",
        "feedback_whole_word",
        "uppercase",
        "card_testing_phrase",
        "card_testing_mixed_case",
        "stolen",
        "disputes",
        "chargebacks",
        "kyc",
        "aml",
        "sanction",
        "sanctions",
        "compliance",
        "settlement",
        "payouts",
        "statement",
        "fee",
        "fees",
        "reserve",
        "compliance_beats_settlement",
        "fraud_beats_settlement",
        "compliance_beats_fee",
        "disputed_is_not_a_keyword",
        "feed_is_not_fee",
        "empty",
    ],
)
def test_classify_escalation_topic_maps_questions_to_the_expected_topic(
    question: str, expected: EscalationTopic
) -> None:
    # Arrange / Act
    topic = classify_escalation_topic(question)

    # Assert
    assert topic == expected


def test_classify_escalation_topic_is_case_insensitive() -> None:
    # Arrange
    question = "WHY WAS MY PAYOUT SHORT?"

    # Act
    topic = classify_escalation_topic(question)

    # Assert
    assert topic == "settlement"
