"""Tests for investigate_transfer_rca (SQ2 root cause by the fraud, compliance and settlement agents), written
from docs/specs/r2-agents.md sections 0 and 4.2 (hand-built data)."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from ops_ai.agents.bus import load_messages
from ops_ai.agents.messages import AgentMessage, message_to_json
from ops_ai.agents.rca import TransferRca, investigate_transfer_rca
from ops_ai.agents.tools import AGENT_TOOLS
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.scope import Scope, UserScope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
SCOPE = UserScope(user_id=SENDER)
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
DECIDED_AT = NOW - timedelta(hours=3)
XB_RULE = "xb_limit_kyc_tier1_v3"
XB_REASON = "cross_border_limit_kyc_tier"
HOLD_RULE = "velocity_rule_9"
HOLD_REASON = "risk_review_pending"
QUESTION = "My international transfer was declined even though I had enough money - what triggered it?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"

INSERT = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, :amount_minor, :currency, :method, :status, :country_sender, "
    ":country_receiver, :reason_code, :rule_id, :kyc_tier, :limit_minor, :limit_currency, "
    ":next_tier_limit_minor, :sender_balance_minor, :decided_at, :ingest_source)"
)
INSERT_KYC = text(
    "INSERT INTO ops_ai.kyc_profiles (user_id, kyc_status, country, ingest_source) VALUES "
    "(:user_id, :kyc_status, :country, 'seed')"
)
SELECT_PERSISTED = text(
    "SELECT message_id, sequence, merchant_id, user_id, created_at, payload::text AS payload, "
    "evidence_refs::text AS evidence_refs FROM ops_ai.agent_messages WHERE correlation_id = :cid "
    "ORDER BY sequence"
)
COUNT_PERSISTED = text("SELECT count(*) FROM ops_ai.agent_messages")


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


def _insert(session_factory: sessionmaker[Session], **overrides: object) -> uuid.UUID:
    params: dict[str, object] = {
        "transfer_id": uuid.uuid4(),
        "sender_user_id": SENDER,
        "amount_minor": 180_000,
        "currency": "USD",
        "method": "bank_transfer",
        "status": "failed",
        "country_sender": "US",
        "country_receiver": "IN",
        "reason_code": XB_REASON,
        "rule_id": XB_RULE,
        "kyc_tier": 1,
        "limit_minor": 100_000,
        "limit_currency": "USD",
        "next_tier_limit_minor": 1_000_000,
        "sender_balance_minor": 525_000,
        "decided_at": DECIDED_AT,
        "ingest_source": "seed",
    }
    params.update(overrides)
    with session_factory.begin() as session:
        session.execute(INSERT, params)
    transfer_id = params["transfer_id"]
    assert isinstance(transfer_id, uuid.UUID)
    return transfer_id


def _kyc(
    session_factory: sessionmaker[Session],
    user_id: uuid.UUID = SENDER,
    kyc_status: str = "verified",
    country: str = "US",
) -> None:
    with session_factory.begin() as session:
        session.execute(INSERT_KYC, {"user_id": user_id, "kyc_status": kyc_status, "country": country})


def _rca(
    session_factory: sessionmaker[Session],
    transfer_id: uuid.UUID | None,
    llm: RecordingLlm | DownLlm | None = None,
    scope: UserScope = SCOPE,
    transport: RecordingTransport | None = None,
) -> TransferRca | None:
    with session_factory() as session:
        result = investigate_transfer_rca(
            session, scope, QUESTION, transfer_id, NOW, llm or RecordingLlm(), transport
        )
        session.commit()
    return result


def _found(
    session_factory: sessionmaker[Session],
    transfer_id: uuid.UUID | None,
    llm: RecordingLlm | DownLlm | None = None,
    transport: RecordingTransport | None = None,
) -> TransferRca:
    result = _rca(session_factory, transfer_id, llm, transport=transport)
    assert isinstance(result, TransferRca)
    return result


def _trail(result: TransferRca) -> list[tuple[int, str, str, str, dict[str, Any]]]:
    return [(m.sequence, m.sender, m.recipient, m.intent, dict(m.payload)) for m in result.messages]


def _everything(result: TransferRca, llm: RecordingLlm | DownLlm) -> str:
    """Every string the result, its messages and the llm requests carry, lower-cased."""
    parts = [repr(result), repr(llm.calls), *(message_to_json(m) for m in result.messages)]
    return " ".join(parts).lower()


# --- the failed SQ2 transfer: the six-message trail ------------------------------------------------------


def test_sq2_produces_exactly_the_six_message_trail_of_the_spec(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    tid = str(transfer_id)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert _trail(result) == [
        (1, "supervisor", "fraud", "assess_transfer", {"transfer_id": tid}),
        (2, "fraud", "compliance", "check_kyc", {"transfer_id": tid, "fraud_hold": False}),
        (3, "fraud", "supervisor", "finding", {"fraud_hold": False}),
        (4, "compliance", "settlement", "verify_funds", {"transfer_id": tid}),
        (5, "settlement", "compliance", "funds_verified", {"balance_covered_amount": True}),
        (
            6,
            "compliance",
            "supervisor",
            "root_cause",
            {
                "cause_code": "cross_border_limit_for_kyc_tier",
                "remediation_owner": "sender",
                "kyc_status": "verified",
                "kyc_tier": 1,
                "cross_border": True,
            },
        ),
    ]


def test_the_trail_shares_one_correlation_id_and_stamps_the_injected_time(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert {m.correlation_id for m in result.messages} == {result.correlation_id}
    assert {m.created_at for m in result.messages} == {NOW}
    assert len({m.message_id for m in result.messages}) == 6


def test_sq2_names_the_cause_the_owner_and_the_tier_upgrade_fix(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert (result.cause_code, result.remediation_owner) == ("cross_border_limit_for_kyc_tier", "sender")
    assert len(result.suggested_fixes) == 1
    assert "tier 2" in result.suggested_fixes[0].lower()
    assert result.truncated is False


def test_sq2_has_a_contribution_from_fraud_compliance_and_settlement_in_that_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert [c.agent for c in result.contributions] == ["fraud", "compliance", "settlement"]
    assert next(c for c in result.contributions if c.agent == "compliance").status == "finding"
    for contribution in result.contributions:
        assert {call.name for call in contribution.tool_calls} <= AGENT_TOOLS[contribution.agent]


def test_the_compliance_agent_reads_the_kyc_profile(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    compliance = next(c for c in result.contributions if c.agent == "compliance")
    assert "kyc_profile" in {call.name for call in compliance.tool_calls}


def test_the_kyc_status_in_the_root_cause_comes_from_the_kyc_profile(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory, kyc_status="pending")

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    root_cause = _trail(result)[5]
    assert root_cause[3] == "root_cause"
    assert root_cause[4]["kyc_status"] == "pending"
    assert root_cause[4]["cause_code"] == "cross_border_limit_for_kyc_tier"


def test_a_domestic_insufficient_funds_transfer_reports_no_cross_border_and_funds_not_covered(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(
        session_factory,
        reason_code="insufficient_funds",
        rule_id=None,
        country_receiver="US",
        sender_balance_minor=40_000,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
    )
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    trail = _trail(result)
    assert [(seq, intent) for seq, _s, _r, intent, _p in trail] == [
        (1, "assess_transfer"),
        (2, "check_kyc"),
        (3, "finding"),
        (4, "verify_funds"),
        (5, "funds_verified"),
        (6, "root_cause"),
    ]
    assert trail[4][4] == {"balance_covered_amount": False}
    assert trail[5][4]["cause_code"] == "insufficient_funds"
    assert trail[5][4]["remediation_owner"] == "sender"
    assert trail[5][4]["cross_border"] is False
    assert (result.cause_code, result.remediation_owner) == ("insufficient_funds", "sender")


def test_the_default_transfer_without_an_id_is_the_newest_failed_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory, decided_at=DECIDED_AT - timedelta(hours=2))
    newest = _insert(session_factory)
    _insert(session_factory, status="success", reason_code=None, decided_at=DECIDED_AT + timedelta(hours=1))
    _kyc(session_factory)

    # Act
    result = _found(session_factory, None)

    # Assert
    assert _trail(result)[0][4] == {"transfer_id": str(newest)}


# --- what the sender must never see ----------------------------------------------------------------------


def test_neither_the_rule_id_nor_the_reason_code_appears_anywhere_in_the_result(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    llm = RecordingLlm()

    # Act
    result = _found(session_factory, transfer_id, llm)

    # Assert
    seen = _everything(result, llm)
    assert XB_RULE not in seen
    assert "xb_limit" not in seen
    assert XB_REASON not in seen
    assert "rule_id" not in seen
    assert "reason_code" not in seen


def test_neither_the_rule_id_nor_the_reason_code_is_persisted(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    result = _found(session_factory, transfer_id)

    # Act
    with session_factory() as session:
        rows = session.execute(SELECT_PERSISTED, {"cid": result.correlation_id}).mappings().all()

    # Assert
    stored = " ".join(f"{row['payload']} {row['evidence_refs']}" for row in rows).lower()
    assert len(rows) == 6
    assert XB_RULE not in stored
    assert XB_REASON not in stored
    assert "rule_id" not in stored
    assert "reason_code" not in stored


def test_no_message_payload_key_names_a_rule_or_a_reason_code(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    keys = {key for _seq, _s, _r, _i, payload in _trail(result) for key in payload}
    assert keys == {
        "transfer_id",
        "fraud_hold",
        "balance_covered_amount",
        "cause_code",
        "remediation_owner",
        "kyc_status",
        "kyc_tier",
        "cross_border",
    }


# --- a transfer that did not fail, including a fraud hold ------------------------------------------------


@pytest.mark.parametrize("status", ["flagged", "success", "pending", "reversed"])
def test_a_transfer_that_did_not_fail_gets_only_the_fraud_assessment_and_cause_not_declined(
    session_factory: sessionmaker[Session], status: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status=status, reason_code=None, rule_id=None)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert _trail(result) == [
        (1, "supervisor", "fraud", "assess_transfer", {"transfer_id": str(transfer_id)}),
        (2, "fraud", "supervisor", "finding", {"fraud_hold": False}),
    ]
    assert (result.cause_code, result.remediation_owner) == ("not_declined", "unknown")
    assert result.suggested_fixes == ()


def test_a_flagged_transfer_never_reveals_the_word_flagged_or_its_rule(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status="flagged", reason_code=HOLD_REASON, rule_id=HOLD_RULE)
    _kyc(session_factory)
    llm = RecordingLlm()

    # Act
    result = _found(session_factory, transfer_id, llm)

    # Assert
    seen = _everything(result, llm)
    assert "flagged" not in seen
    assert HOLD_RULE not in seen
    assert HOLD_REASON not in seen
    assert (result.cause_code, result.remediation_owner) == ("not_declined", "unknown")


def test_a_flagged_transfer_is_persisted_without_the_word_flagged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status="flagged", reason_code=HOLD_REASON, rule_id=HOLD_RULE)
    result = _found(session_factory, transfer_id)

    # Act
    with session_factory() as session:
        rows = session.execute(SELECT_PERSISTED, {"cid": result.correlation_id}).mappings().all()

    # Assert
    stored = " ".join(f"{row['payload']} {row['evidence_refs']}" for row in rows).lower()
    assert len(rows) == 2
    assert "flagged" not in stored
    assert HOLD_RULE not in stored


def test_a_flagged_transfer_looks_the_same_as_a_pending_one_in_the_trail(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    flagged = _insert(session_factory, status="flagged", reason_code=HOLD_REASON, rule_id=HOLD_RULE)
    pending = _insert(session_factory, status="pending", reason_code=None, rule_id=None)

    # Act
    flagged_result = _found(session_factory, flagged)
    pending_result = _found(session_factory, pending)

    # Assert
    def shape(result: TransferRca, transfer_id: uuid.UUID) -> list[tuple[int, str, str, str, str]]:
        return [
            (seq, sender, recipient, intent, str(sorted(payload.items())).replace(str(transfer_id), "ID"))
            for seq, sender, recipient, intent, payload in _trail(result)
        ]

    assert shape(flagged_result, flagged) == shape(pending_result, pending)
    assert flagged_result.cause_code == pending_result.cause_code == "not_declined"


# --- not found for this sender ---------------------------------------------------------------------------


def test_another_senders_transfer_gives_none_and_no_llm_call_and_no_messages(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    others = _insert(session_factory, sender_user_id=OTHER_SENDER)
    llm = RecordingLlm()

    # Act
    result = _rca(session_factory, others, llm)

    # Assert
    assert result is None
    assert llm.calls == []
    with session_factory() as session:
        assert session.execute(COUNT_PERSISTED).scalar_one() == 0


def test_a_missing_transfer_id_gives_none(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert(session_factory)
    llm = RecordingLlm()

    # Act
    result = _rca(session_factory, uuid.UUID("deadbeef-0000-4000-8000-000000000000"), llm)

    # Assert
    assert result is None
    assert llm.calls == []


def test_no_id_and_no_failed_transfer_gives_none(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert(session_factory, status="success", reason_code=None)
    _insert(session_factory, sender_user_id=OTHER_SENDER)

    # Act
    result = _rca(session_factory, None)

    # Assert
    assert result is None


# --- persistence, transport, llm -------------------------------------------------------------------------


def test_the_messages_are_persisted_with_the_user_id_and_no_merchant_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    with session_factory() as session:
        rows = session.execute(SELECT_PERSISTED, {"cid": result.correlation_id}).mappings().all()
    assert [(row["sequence"], row["merchant_id"], row["user_id"]) for row in rows] == [
        (message.sequence, None, SENDER) for message in result.messages
    ]
    assert [row["message_id"] for row in rows] == [message.message_id for message in result.messages]
    assert {row["created_at"] for row in rows} == {NOW}


def test_the_persisted_trail_is_not_visible_to_a_merchant_scope(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    result = _found(session_factory, transfer_id)

    # Act
    with session_factory() as session:
        loaded = load_messages(session, Scope(merchant_id=uuid.UUID(int=5)), result.correlation_id)

    # Assert
    assert loaded == []


def test_a_transport_receives_every_message_in_order(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    transport = RecordingTransport()

    # Act
    result = _found(session_factory, transfer_id, transport=transport)

    # Assert
    assert tuple(transport.published) == tuple(result.messages)
    assert len(transport.published) == 6


def test_the_llm_receives_the_question_and_exactly_the_evidence_and_its_narration_is_returned(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    llm = RecordingLlm()

    # Act
    result = _found(session_factory, transfer_id, llm)

    # Assert
    assert len(llm.calls) == 1
    assert llm.calls[0].question == QUESTION
    assert llm.calls[0].evidence == tuple(result.evidence)
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)


def test_an_unavailable_llm_gives_t2_fallback_with_the_same_findings(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)
    with_llm = _found(session_factory, transfer_id, RecordingLlm())
    down = DownLlm()

    # Act
    fallback = _found(session_factory, transfer_id, down)

    # Assert
    assert (fallback.mode, fallback.text, fallback.model) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert fallback.cause_code == with_llm.cause_code
    assert fallback.remediation_owner == with_llm.remediation_owner
    assert fallback.suggested_fixes == with_llm.suggested_fixes
    assert fallback.evidence == with_llm.evidence
    assert [t[1:] for t in _trail(fallback)] == [t[1:] for t in _trail(with_llm)]


def test_each_investigation_gets_a_new_correlation_id(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    _kyc(session_factory)

    # Act
    first = _found(session_factory, transfer_id)
    second = _found(session_factory, transfer_id)

    # Assert
    assert first.correlation_id != second.correlation_id
