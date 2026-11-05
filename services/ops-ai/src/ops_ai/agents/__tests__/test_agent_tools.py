"""Tests for the per-agent tool allowlists and the AgentToolbox, written from docs/specs/r2-agents.md
section 4 (`tools.py`)."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ops_ai.agents.kyc import KycProfileView
from ops_ai.agents.messages import AgentName
from ops_ai.agents.tools import AGENT_TOOLS, AgentToolbox, ToolNotAllowedError
from ops_ai.feedback.thresholds import BASELINE
from ops_ai.scope import Scope, UserScope
from ops_ai.transfers import TransferStatus
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
START = NOW - timedelta(hours=24)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
USER_SCOPE = UserScope(user_id=SENDER)
TRANSFER_ID = uuid.UUID("dddddddd-0000-4000-8000-000000000001")
AGENTS: tuple[AgentName, ...] = ("supervisor", "fraud", "settlement", "dispute", "compliance")

Call = Callable[[AgentToolbox], Any]
CALLS: dict[str, Call] = {
    "analyze_fraud": lambda toolbox: toolbox.analyze_fraud(SCOPE, START, NOW),
    "fraud_rule_version": lambda toolbox: toolbox.fraud_rule_version(SCOPE),
    "explain_settlement": lambda toolbox: toolbox.explain_settlement(SCOPE, None),
    "dispute_tickets": lambda toolbox: toolbox.dispute_tickets(SCOPE),
    "transfer_status": lambda toolbox: toolbox.transfer_status(USER_SCOPE, TRANSFER_ID),
    "kyc_profile": lambda toolbox: toolbox.kyc_profile(USER_SCOPE),
}
ALL_TOOLS = frozenset(CALLS)
DISALLOWED = [(agent, tool) for agent in AGENTS for tool in sorted(ALL_TOOLS - AGENT_TOOLS[agent])]
ALLOWED_ORDERS = [
    (agent, order)
    for agent in AGENTS
    if AGENT_TOOLS[agent]
    for order in (sorted(AGENT_TOOLS[agent]), sorted(AGENT_TOOLS[agent], reverse=True))
]

INSERT_TRANSFER = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, 180000, 'USD', 'bank_transfer', 'failed', 'US', 'IN', "
    "'cross_border_limit_kyc_tier', 'xb_limit_kyc_tier1_v3', 1, 100000, 'USD', 1000000, 525000, :decided_at, "
    "'seed')"
)
INSERT_KYC = text(
    "INSERT INTO ops_ai.kyc_profiles (user_id, kyc_status, country, ingest_source) VALUES "
    "(:user_id, :kyc_status, :country, 'seed')"
)
INSERT_RULE_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
    "(:merchant_id, :version, :steps, :min_micro_payments, :min_micro_share, :min_distinct_cards, "
    "'feedback:false_positive', NULL, :created_at)"
)


# --- the allowlists --------------------------------------------------------------------------------------


def test_agent_tools_are_exactly_the_specified_allowlists() -> None:
    # Arrange
    expected = {
        "supervisor": frozenset(),
        "fraud": frozenset({"analyze_fraud", "fraud_rule_version", "transfer_status"}),
        "settlement": frozenset({"explain_settlement", "transfer_status"}),
        "dispute": frozenset({"dispute_tickets", "explain_settlement"}),
        "compliance": frozenset({"kyc_profile", "transfer_status"}),
    }

    # Act
    actual = dict(AGENT_TOOLS)

    # Assert
    assert actual == expected


def test_every_allowlisted_tool_is_a_toolbox_method_the_test_knows() -> None:
    # Arrange
    listed = frozenset().union(*AGENT_TOOLS.values())

    # Act
    unknown = listed - ALL_TOOLS

    # Assert
    assert unknown == frozenset()


# --- disallowed tools ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("agent", "tool"), DISALLOWED, ids=[f"{a}.{t}" for a, t in DISALLOWED])
def test_calling_a_tool_outside_the_allowlist_raises_tool_not_allowed_and_records_nothing(
    session_factory: sessionmaker[Session], agent: AgentName, tool: str
) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox(agent, session)

        # Act
        with pytest.raises(ToolNotAllowedError):
            CALLS[tool](toolbox)

        # Assert
        assert toolbox.tool_calls == []
        assert session.in_transaction() is False


def test_the_supervisor_has_no_tool_at_all() -> None:
    # Arrange
    allowed = AGENT_TOOLS["supervisor"]

    # Act
    disallowed = [tool for agent, tool in DISALLOWED if agent == "supervisor"]

    # Assert
    assert allowed == frozenset()
    assert sorted(disallowed) == sorted(ALL_TOOLS)


def test_compliance_cannot_run_the_fraud_analysis(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox("compliance", session)

        # Act
        with pytest.raises(ToolNotAllowedError):
            toolbox.analyze_fraud(SCOPE, START, NOW)

        # Assert
        assert toolbox.tool_calls == []


def test_a_refused_call_does_not_stop_a_later_allowed_call_from_being_recorded(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox("compliance", session)
        with pytest.raises(ToolNotAllowedError):
            toolbox.explain_settlement(SCOPE, None)

        # Act
        toolbox.kyc_profile(USER_SCOPE)

        # Assert
        assert [call.name for call in toolbox.tool_calls] == ["kyc_profile"]


# --- allowed tools ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("agent", "order"), ALLOWED_ORDERS, ids=[f"{a}:{'>'.join(o)}" for a, o in ALLOWED_ORDERS]
)
def test_allowed_calls_are_recorded_in_call_order(
    session_factory: sessionmaker[Session], agent: AgentName, order: list[str]
) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox(agent, session)

        # Act
        for tool in order:
            CALLS[tool](toolbox)

        # Assert
        assert [call.name for call in toolbox.tool_calls] == order


def test_a_fresh_toolbox_has_recorded_no_calls(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        toolbox = AgentToolbox("fraud", session)

        # Assert
        assert toolbox.tool_calls == []


def test_the_same_tool_called_twice_is_recorded_twice(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox("dispute", session)

        # Act
        toolbox.dispute_tickets(SCOPE)
        toolbox.dispute_tickets(SCOPE)

        # Assert
        assert [call.name for call in toolbox.tool_calls] == ["dispute_tickets", "dispute_tickets"]


def test_two_toolboxes_keep_separate_call_records(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        fraud = AgentToolbox("fraud", session)
        compliance = AgentToolbox("compliance", session)

        # Act
        fraud.fraud_rule_version(SCOPE)
        compliance.kyc_profile(USER_SCOPE)

        # Assert
        assert [call.name for call in fraud.tool_calls] == ["fraud_rule_version"]
        assert [call.name for call in compliance.tool_calls] == ["kyc_profile"]


def test_fraud_rule_version_is_the_baseline_without_a_stored_version(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox("fraud", session)

        # Act
        rule = toolbox.fraud_rule_version(SCOPE)

        # Assert
        assert rule == BASELINE


def test_fraud_rule_version_is_the_newest_version_of_the_scope_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for version, steps, min_micro, share, cards in ((1, 1, 12, 0.83, 4), (2, 2, 14, 0.86, 5)):
            session.execute(
                INSERT_RULE_VERSION,
                {
                    "merchant_id": MERCHANT,
                    "version": version,
                    "steps": steps,
                    "min_micro_payments": min_micro,
                    "min_micro_share": share,
                    "min_distinct_cards": cards,
                    "created_at": NOW,
                },
            )
    with session_factory() as session:
        toolbox = AgentToolbox("fraud", session)

        # Act
        rule = toolbox.fraud_rule_version(SCOPE)

        # Assert
        assert (rule.version, rule.steps, rule.min_micro_payments, rule.min_distinct_cards) == (2, 2, 14, 5)
        assert rule.min_micro_share == 0.86


def test_analyze_fraud_on_an_empty_history_has_no_episodes(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox("fraud", session)

        # Act
        analysis = toolbox.analyze_fraud(SCOPE, START, NOW)

        # Assert
        assert analysis.episodes == ()
        assert analysis.micro_payments == 0
        assert (analysis.start, analysis.end) == (START, NOW)


def test_explain_settlement_without_entries_is_none(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        toolbox = AgentToolbox("settlement", session)

        # Act
        statement = toolbox.explain_settlement(SCOPE, None)

        # Assert
        assert statement is None


def test_transfer_status_returns_the_scope_users_own_transfer_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        session.execute(
            INSERT_TRANSFER,
            {"transfer_id": TRANSFER_ID, "sender_user_id": SENDER, "decided_at": NOW},
        )
    with session_factory() as session:
        toolbox = AgentToolbox("compliance", session)

        # Act
        own = toolbox.transfer_status(USER_SCOPE, TRANSFER_ID)
        others = toolbox.transfer_status(UserScope(user_id=OTHER_SENDER), TRANSFER_ID)

        # Assert
        assert isinstance(own, TransferStatus)
        assert (own.transfer_id, own.amount_minor, own.status) == (TRANSFER_ID, 180_000, "failed")
        assert others is None


def test_kyc_profile_returns_the_scope_users_own_profile_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        session.execute(INSERT_KYC, {"user_id": SENDER, "kyc_status": "verified", "country": "US"})
        session.execute(INSERT_KYC, {"user_id": OTHER_SENDER, "kyc_status": "failed", "country": "IN"})
    with session_factory() as session:
        toolbox = AgentToolbox("compliance", session)

        # Act
        own = toolbox.kyc_profile(USER_SCOPE)
        stranger = toolbox.kyc_profile(UserScope(user_id=uuid.UUID(int=77)))

        # Assert
        assert own == KycProfileView(user_id=SENDER, kyc_status="verified", country="US")
        assert stranger is None
