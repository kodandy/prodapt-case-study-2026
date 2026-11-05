"""Tests for plan_agents and investigate_with_agents (merchant investigation by fraud, settlement and dispute
agents), written from docs/specs/r2-agents.md sections 0, 1 and 4.1."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ops_ai.agents.bus import MAX_MESSAGES, load_messages
from ops_ai.agents.messages import AgentMessage
from ops_ai.agents.supervisor import AgentInvestigation, investigate_with_agents, plan_agents
from ops_ai.agents.tools import AGENT_TOOLS
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
BURST_START = NOW - timedelta(hours=2)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
CARD_QUESTION = "Is this card testing?"
ALL_QUESTION = "Is it fraud, why is my payout short, and is there a dispute?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
PERIOD = "2026-10"
CAPTURED_AT = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, 'USD', 'card', :state, 'acq_a', NULL, :card_bin, :card_ref, "
    "1, :occurred_at)"
)
INSERT_ENTRY = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, 'USD', :captured_at, :period, false, :chargeback, 'seed')"
)
INSERT_DISPUTE_TICKET = text(
    "INSERT INTO ops_ai.tickets (ticket_id, source_key, source, queue, priority, merchant_id, title, reason, "
    "created_at) VALUES (:ticket_id, :source_key, 'dispute', 'payments_ops', 'normal', :merchant_id, "
    "'Chargeback received', 'Chargeback on a payment', :created_at)"
)
INSERT_RULE_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
    "(:merchant_id, :version, :steps, :min_micro_payments, :min_micro_share, :min_distinct_cards, "
    "'feedback:false_positive', NULL, :created_at)"
)
SELECT_PERSISTED = text(
    "SELECT sequence, merchant_id, user_id FROM ops_ai.agent_messages WHERE correlation_id = :cid "
    "ORDER BY sequence"
)


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


@dataclass
class RecordingTransport:
    published: list[AgentMessage] = field(default_factory=list)

    def publish(self, message: AgentMessage) -> None:
        self.published.append(message)


def _card_ref(n: int) -> str:
    return f"{n:032x}"


def _burst(
    session_factory: sessionmaker[Session],
    *,
    merchant_id: uuid.UUID = MERCHANT,
    first_n: int = 1,
    count: int = 12,
    cards: int = 4,
    declined: int = 6,
) -> list[uuid.UUID]:
    """A card-testing burst: 12 payments of 0.50 USD ten seconds apart on 4 cards, 6 of them declined."""
    ids = [uuid.UUID(int=first_n + index) for index in range(count)]
    with session_factory.begin() as session:
        for index, payment_id in enumerate(ids):
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": 50,
                    "state": "declined" if index < declined else "succeeded",
                    "card_bin": "400000",
                    "card_ref": _card_ref(index % cards + 1),
                    "occurred_at": BURST_START + timedelta(seconds=index * 10),
                },
            )
    return ids


def _rule_version(
    session_factory: sessionmaker[Session],
    *,
    merchant_id: uuid.UUID = MERCHANT,
    version: int,
    steps: int,
    min_micro_payments: int,
    min_micro_share: float,
    min_distinct_cards: int,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_RULE_VERSION,
            {
                "merchant_id": merchant_id,
                "version": version,
                "steps": steps,
                "min_micro_payments": min_micro_payments,
                "min_micro_share": min_micro_share,
                "min_distinct_cards": min_distinct_cards,
                "created_at": NOW,
            },
        )


def _settlement(session_factory: sessionmaker[Session], *, merchant_id: uuid.UUID = MERCHANT) -> None:
    """Three October captures of 100.00 USD, 100.00 USD and 50.00 USD; the last one was charged back."""
    captures = ((9001, 10_000, False), (9002, 10_000, False), (9003, 5_000, True))
    with session_factory.begin() as session:
        for n, amount, chargeback in captures:
            payment_id = uuid.uuid5(merchant_id, str(n))
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": amount,
                    "state": "succeeded",
                    "card_bin": "424242",
                    "card_ref": _card_ref(500 + n),
                    "occurred_at": CAPTURED_AT,
                },
            )
            session.execute(
                INSERT_ENTRY,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": amount,
                    "captured_at": CAPTURED_AT,
                    "period": PERIOD,
                    "chargeback": chargeback,
                },
            )


def _dispute_ticket(session_factory: sessionmaker[Session], *, merchant_id: uuid.UUID = MERCHANT) -> None:
    ticket_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_DISPUTE_TICKET,
            {
                "ticket_id": ticket_id,
                "source_key": f"dispute:{ticket_id}",
                "merchant_id": merchant_id,
                "created_at": CAPTURED_AT,
            },
        )


def _run(
    session_factory: sessionmaker[Session],
    question: str = CARD_QUESTION,
    llm: RecordingLlm | DownLlm | None = None,
    scope: Scope = SCOPE,
    transport: RecordingTransport | None = None,
) -> AgentInvestigation:
    with session_factory() as session:
        result = investigate_with_agents(session, scope, question, NOW, llm or RecordingLlm(), transport)
        session.commit()
    return result


def _contribution(result: AgentInvestigation, agent: str) -> Any:
    return next(contribution for contribution in result.contributions if contribution.agent == agent)


def _triples(messages: Sequence[AgentMessage]) -> set[tuple[str, str, str]]:
    return {(message.sender, message.recipient, message.intent) for message in messages}


def _payload_of(result: AgentInvestigation, intent: str) -> dict[str, Any]:
    matching = [dict(message.payload) for message in result.messages if message.intent == intent]
    assert len(matching) == 1
    return matching[0]


# --- plan_agents -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Is this card testing?", ("fraud",)),
        ("Looks like Card-Testing to me", ("fraud",)),
        ("Why so many micro charges?", ("fraud",)),
        ("Any suspicious activity?", ("fraud",)),
        ("Is this FRAUD?", ("fraud",)),
        ("Why is my payout short?", ("settlement",)),
        ("What was deducted from the settlement?", ("settlement",)),
        ("Which fee was charged?", ("settlement",)),
        ("PAYOUT", ("settlement",)),
        ("Did we get a chargeback?", ("dispute",)),
        ("Any dispute open?", ("dispute",)),
        ("Is it fraud and why is my payout short?", ("fraud", "settlement")),
        ("payout, fee and chargeback status", ("settlement", "dispute")),
        ("dispute? fee? fraud?", ("fraud", "settlement", "dispute")),
        ("Hello", ("fraud", "settlement", "dispute")),
        ("", ("fraud", "settlement", "dispute")),
        ("What is the weather today?", ("fraud", "settlement", "dispute")),
    ],
    ids=[
        "card_testing",
        "hyphenated",
        "micro",
        "suspicious",
        "fraud_upper_case",
        "payout",
        "settlement_deducted",
        "fee",
        "payout_upper_case",
        "chargeback",
        "dispute",
        "fraud_and_payout",
        "settlement_and_dispute",
        "all_three_in_plan_order",
        "hello_means_all",
        "empty_means_all",
        "unrelated_means_all",
    ],
)
def test_plan_agents_picks_agents_by_keyword_in_the_fixed_order(
    question: str, expected: tuple[str, ...]
) -> None:
    # Arrange
    # (the question is the only input)

    # Act
    plan = plan_agents(question)

    # Assert
    assert plan == expected


# --- flagged card-testing burst --------------------------------------------------------------------------


def test_a_card_testing_burst_gives_a_fraud_finding_citing_the_episode(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    fraud = _contribution(result, "fraud")
    assert result.plan == ("fraud",)
    assert fraud.status == "finding"
    assert [item.ref for item in fraud.evidence] == [f"episode:{ids[0]}"]
    assert f"episode:{ids[0]}" in [item.ref for item in result.evidence]


def test_a_flagged_burst_sends_check_disputes_to_the_dispute_agent(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert _triples(result.messages) >= {
        ("supervisor", "fraud", "investigate"),
        ("fraud", "dispute", "check_disputes"),
        ("fraud", "supervisor", "finding"),
        ("dispute", "settlement", "chargeback_impact"),
        ("settlement", "dispute", "chargeback_impact_result"),
        ("dispute", "supervisor", "finding"),
    }


def test_a_flagged_burst_with_disputes_and_a_statement_gives_dispute_and_settlement_contributions(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert sorted(contribution.agent for contribution in result.contributions) == [
        "dispute",
        "fraud",
        "settlement",
    ]
    dispute = _contribution(result, "dispute")
    assert dispute.status == "finding"
    assert f"disputes:{MERCHANT}" in [item.ref for item in dispute.evidence]


def test_the_settlement_agent_replies_with_the_chargeback_totals_and_period(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    payload = _payload_of(result, "chargeback_impact_result")
    assert set(payload) == {"chargebacks_minor", "chargeback_fees_minor", "period"}
    assert abs(payload["chargebacks_minor"]) == 5_000
    assert abs(payload["chargeback_fees_minor"]) == 1_500
    assert payload["period"] == PERIOD


def test_the_settlement_agent_replies_zero_and_no_period_without_a_statement(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert _payload_of(result, "chargeback_impact_result") == {
        "chargebacks_minor": 0,
        "chargeback_fees_minor": 0,
        "period": None,
    }


def test_the_dispute_agent_finds_chargebacks_in_the_statement_even_without_a_dispute_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    dispute = _contribution(result, "dispute")
    assert dispute.status == "finding"
    assert [item.ref for item in dispute.evidence] == [f"disputes:{MERCHANT}"]


def test_the_dispute_agent_finds_a_dispute_ticket_even_without_a_statement(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    dispute = _contribution(result, "dispute")
    assert dispute.status == "finding"
    assert [item.ref for item in dispute.evidence] == [f"disputes:{MERCHANT}"]


def test_the_dispute_agent_has_no_finding_without_tickets_or_chargebacks(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    dispute = _contribution(result, "dispute")
    assert dispute.status == "no_finding"
    assert dispute.evidence == ()


def test_the_dispute_agent_works_only_the_first_request_when_it_gets_two(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: fraud plans dispute too, and then sends it check_disputes as well
    _burst(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory, question="Is it fraud and is there a dispute?")

    # Assert
    assert result.plan == ("fraud", "dispute")
    assert [message.intent for message in result.messages].count("chargeback_impact") == 1
    assert [message.intent for message in result.messages].count("chargeback_impact_result") == 1
    dispute = _contribution(result, "dispute")
    assert [call.name for call in dispute.tool_calls] == ["dispute_tickets"]


def test_message_sequences_are_contiguous_from_one_and_share_the_correlation_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert [message.sequence for message in result.messages] == list(range(1, len(result.messages) + 1))
    assert {message.correlation_id for message in result.messages} == {result.correlation_id}
    first = result.messages[0]
    assert (first.sender, first.recipient, first.intent) == ("supervisor", "fraud", "investigate")


def test_a_normal_investigation_stays_inside_the_message_budget(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory, question=ALL_QUESTION)

    # Assert
    assert len(result.messages) <= MAX_MESSAGES
    assert result.truncated is False


# --- the fraud agent honours the active rule version -----------------------------------------------------


def test_the_fraud_agent_reads_the_rule_version_and_analyses_the_24_hours_before_now(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    fraud = _contribution(result, "fraud")
    assert sorted(call.name for call in fraud.tool_calls) == ["analyze_fraud", "fraud_rule_version"]


def test_a_rule_version_with_steps_five_stops_flagging_the_twelve_payment_burst(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: steps 5 -> at least 20 micro payments, 95 % share, 8 distinct cards
    _burst(session_factory)
    _rule_version(
        session_factory,
        version=1,
        steps=5,
        min_micro_payments=20,
        min_micro_share=0.95,
        min_distinct_cards=8,
    )

    # Act
    result = _run(session_factory)

    # Assert
    fraud = _contribution(result, "fraud")
    assert fraud.status == "no_finding"
    assert fraud.evidence == ()
    assert "check_disputes" not in [message.intent for message in result.messages]
    assert f"episode:{uuid.UUID(int=1)}" not in [item.ref for item in result.evidence]


def test_an_unflagged_fraud_agent_still_reports_to_the_supervisor(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _rule_version(
        session_factory,
        version=1,
        steps=5,
        min_micro_payments=20,
        min_micro_share=0.95,
        min_distinct_cards=8,
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert [(m.sender, m.recipient, m.intent) for m in result.messages] == [
        ("supervisor", "fraud", "investigate"),
        ("fraud", "supervisor", "finding"),
    ]


def test_a_rule_version_that_still_covers_the_burst_flags_it_and_the_summary_names_the_version(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: steps 1 -> at least 12 micro payments, 83 % share, 4 distinct cards: the burst meets all three
    _burst(session_factory)
    _rule_version(
        session_factory,
        version=1,
        steps=1,
        min_micro_payments=12,
        min_micro_share=0.83,
        min_distinct_cards=4,
    )

    # Act
    result = _run(session_factory)

    # Assert
    fraud = _contribution(result, "fraud")
    assert fraud.status == "finding"
    assert re.search(r"(?i)(?:\bv|version[ =:]*)1\b", fraud.summary) is not None


def test_a_rule_version_between_baseline_and_steps_five_flags_only_what_it_still_covers(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: steps 2 -> at least 14 micro payments, so the 12-payment burst falls below it
    _burst(session_factory)
    _rule_version(
        session_factory,
        version=2,
        steps=2,
        min_micro_payments=14,
        min_micro_share=0.86,
        min_distinct_cards=5,
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert _contribution(result, "fraud").status == "no_finding"


def test_another_merchants_rule_version_does_not_change_this_merchants_flagging(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _rule_version(
        session_factory,
        merchant_id=OTHER_MERCHANT,
        version=1,
        steps=5,
        min_micro_payments=20,
        min_micro_share=0.95,
        min_distinct_cards=8,
    )

    # Act
    result = _run(session_factory)

    # Assert
    assert _contribution(result, "fraud").status == "finding"


# --- settlement and the other plans ----------------------------------------------------------------------


def test_the_settlement_agent_reports_a_statement_with_a_gap_citing_the_period(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _settlement(session_factory)

    # Act
    result = _run(session_factory, question="Why is my payout short?")

    # Assert
    settlement = _contribution(result, "settlement")
    assert result.plan == ("settlement",)
    assert settlement.status == "finding"
    assert [item.ref for item in settlement.evidence] == [f"statement:{PERIOD}"]
    assert [call.name for call in settlement.tool_calls] == ["explain_settlement"]


def test_the_settlement_agent_has_no_finding_without_a_statement(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    # (no settlement entries)

    # Act
    result = _run(session_factory, question="Why is my payout short?")

    # Assert
    settlement = _contribution(result, "settlement")
    assert settlement.status == "no_finding"
    assert settlement.evidence == ()


def test_a_question_without_keywords_runs_all_three_agents_and_orders_the_finding_by_plan(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory, question="Hello")

    # Assert
    assert result.plan == ("fraud", "settlement", "dispute")
    refs = [item.ref for item in result.evidence]
    assert f"episode:{ids[0]}" in refs
    assert f"statement:{PERIOD}" in refs
    assert f"disputes:{MERCHANT}" in refs
    summaries = [_contribution(result, agent).summary for agent in result.plan]
    positions = [result.finding.index(summary) for summary in summaries]
    assert positions == sorted(positions)
    assert len(set(positions)) == 3


def test_an_empty_history_gives_no_finding_for_every_agent(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    # (no payments, entries or tickets)

    # Act
    result = _run(session_factory, question="Hello")

    # Assert
    assert {c.agent: c.status for c in result.contributions} == {
        "fraud": "no_finding",
        "settlement": "no_finding",
        "dispute": "no_finding",
    }
    assert tuple(result.evidence) == ()
    assert result.truncated is False


# --- tools, scope, llm, persistence ----------------------------------------------------------------------


def test_every_contribution_uses_only_its_own_agents_allowlisted_tools(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory, question=ALL_QUESTION)

    # Assert
    assert {c.agent for c in result.contributions} == {"fraud", "settlement", "dispute"}
    for contribution in result.contributions:
        used = {call.name for call in contribution.tool_calls}
        assert used <= AGENT_TOOLS[contribution.agent]
        assert used != set()
    overall = {call.name for call in result.tool_calls}
    assert {"analyze_fraud", "fraud_rule_version", "explain_settlement", "dispute_tickets"} <= overall
    assert overall <= AGENT_TOOLS["fraud"] | AGENT_TOOLS["settlement"] | AGENT_TOOLS["dispute"]


def test_another_merchants_data_is_invisible_to_every_agent(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    other_ids = _burst(session_factory, merchant_id=OTHER_MERCHANT, first_n=1000)
    _settlement(session_factory, merchant_id=OTHER_MERCHANT)
    _dispute_ticket(session_factory, merchant_id=OTHER_MERCHANT)

    # Act
    result = _run(session_factory, question="Hello")

    # Assert
    assert {c.agent: c.status for c in result.contributions} == {
        "fraud": "no_finding",
        "settlement": "no_finding",
        "dispute": "no_finding",
    }
    assert tuple(result.evidence) == ()
    assert str(OTHER_MERCHANT) not in repr(result)
    assert str(other_ids[0]) not in repr(result)


def test_the_llm_receives_the_question_and_exactly_the_combined_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)
    llm = RecordingLlm()

    # Act
    result = _run(session_factory, question=ALL_QUESTION, llm=llm)

    # Assert
    assert len(llm.calls) == 1
    assert llm.calls[0].question == ALL_QUESTION
    assert llm.calls[0].evidence == tuple(result.evidence)
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)


def test_an_unavailable_llm_gives_t2_fallback_with_the_same_findings(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _settlement(session_factory)
    _dispute_ticket(session_factory)
    with_llm = _run(session_factory, question=ALL_QUESTION)
    down = DownLlm()

    # Act
    fallback = _run(session_factory, question=ALL_QUESTION, llm=down)

    # Assert
    assert (fallback.mode, fallback.text, fallback.model) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert fallback.plan == with_llm.plan
    assert fallback.finding == with_llm.finding
    assert fallback.evidence == with_llm.evidence
    assert [c.status for c in fallback.contributions] == [c.status for c in with_llm.contributions]
    assert [(m.sender, m.recipient, m.intent) for m in fallback.messages] == [
        (m.sender, m.recipient, m.intent) for m in with_llm.messages
    ]


def test_the_messages_are_persisted_with_the_merchant_id_and_no_user_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    _dispute_ticket(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    with session_factory() as session:
        rows = session.execute(SELECT_PERSISTED, {"cid": result.correlation_id}).all()
        loaded = load_messages(session, SCOPE, result.correlation_id)
        others = load_messages(session, OTHER_SCOPE, result.correlation_id)
    assert [(row.sequence, row.merchant_id, row.user_id) for row in rows] == [
        (message.sequence, MERCHANT, None) for message in result.messages
    ]
    assert loaded == list(result.messages)
    assert others == []


def test_each_investigation_gets_its_own_correlation_id(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    first = _run(session_factory)
    second = _run(session_factory)

    # Assert
    assert first.correlation_id != second.correlation_id


def test_a_transport_receives_every_message_of_the_investigation_in_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)
    transport = RecordingTransport()

    # Act
    result = _run(session_factory, transport=transport)

    # Assert
    assert tuple(transport.published) == tuple(result.messages)
