"""investigate_fraud applies the merchant's active rule version (R2-7), written from docs/specs/sq3.md
"Feedback-adjusted thresholds ... (11-06)" S-2."""

import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.assistant.llm_port import MockLlm
from ops_ai.clock import Clock
from ops_ai.feedback.thresholds import append_rule_version, thresholds_for_steps
from ops_ai.fraud_watch.service import FraudInvestigation, investigate_fraud
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
BURST_START = NOW - timedelta(hours=2)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
QUESTION = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, 50, 'USD', 'card', :state, 'acq_a', NULL, '400000', :card_ref, 1, "
    ":occurred_at)"
)


def _burst(session_factory: sessionmaker[Session], merchant_id: uuid.UUID, first_n: int) -> list[uuid.UUID]:
    """12 micro USD card payments 10 s apart on 4 cards, the first 6 declined."""
    ids = [uuid.UUID(int=first_n + index) for index in range(12)]
    with session_factory.begin() as session:
        for index, payment_id in enumerate(ids):
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "state": "declined" if index < 6 else "succeeded",
                    "card_ref": f"{first_n * 10 + index % 4 + 1:032x}",
                    "occurred_at": BURST_START + timedelta(seconds=index * 10),
                },
            )
    return ids


def _set_steps(
    session_factory: sessionmaker[Session], clock: Clock, merchant_id: uuid.UUID, steps: int
) -> int:
    with session_factory.begin() as session:
        return append_rule_version(
            session, Scope(merchant_id), clock, steps, thresholds_for_steps(steps), "test:threshold", None
        ).version


def _investigate(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT, question: str = QUESTION
) -> FraudInvestigation:
    with session_factory() as session:
        return investigate_fraud(session, Scope(merchant_id), question, NOW, MockLlm())


def test_baseline_merchant_is_flagged_under_rule_version_zero(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ids = _burst(session_factory, MERCHANT, 1)

    # Act
    result = _investigate(session_factory)

    # Assert
    assert result.cause_label == "card_testing"
    assert result.rule_version == 0
    assert result.analysis is not None
    assert [(e.first_payment_id, e.flagged) for e in result.analysis.episodes] == [(ids[0], True)]
    assert result.evidence[0].ref == f"episode:{ids[0]}"
    assert result.evidence[0].summary.endswith(", fraud rule v0")


def test_one_step_version_still_flags_and_reports_the_version_applied(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    ids = _burst(session_factory, MERCHANT, 1)
    version = _set_steps(session_factory, clock, MERCHANT, 1)

    # Act
    result = _investigate(session_factory)

    # Assert
    assert version == 1
    assert result.cause_label == "card_testing"
    assert result.rule_version == 1
    assert result.evidence[0].ref == f"episode:{ids[0]}"
    assert result.evidence[0].summary.endswith(", fraud rule v1")
    assert [call.name for call in result.tool_calls] == ["analyze_fraud"]


def test_two_step_version_unflags_the_burst_with_no_reasons_fixes_or_evidence(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    ids = _burst(session_factory, MERCHANT, 1)
    _set_steps(session_factory, clock, MERCHANT, 1)
    version = _set_steps(session_factory, clock, MERCHANT, 2)

    # Act
    result = _investigate(session_factory)

    # Assert
    assert version == 2
    assert result.cause_label == "no_card_testing"
    assert result.rule_version == 2
    assert result.analysis is not None
    assert [
        (e.first_payment_id, e.micro_payments, e.flagged, e.reasons) for e in result.analysis.episodes
    ] == [(ids[0], 12, False, ())]
    assert result.alerts == ()
    assert result.suggested_fixes == ()
    assert result.evidence == ()
    assert [call.name for call in result.tool_calls] == ["analyze_fraud"]


def test_another_merchants_version_does_not_change_this_merchants_result(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _burst(session_factory, MERCHANT, 1)
    _burst(session_factory, OTHER_MERCHANT, 1000)
    _set_steps(session_factory, clock, OTHER_MERCHANT, 1)
    _set_steps(session_factory, clock, OTHER_MERCHANT, 2)

    # Act
    own = _investigate(session_factory)
    other = _investigate(session_factory, OTHER_MERCHANT)

    # Assert
    assert (own.cause_label, own.rule_version) == ("card_testing", 0)
    assert (other.cause_label, other.rule_version) == ("no_card_testing", 2)


def test_unsupported_question_reads_no_rule_and_reports_no_version(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, MERCHANT, 1)

    # Act
    result = _investigate(session_factory, question="What is our settlement schedule?")

    # Assert
    assert result.intent == "unsupported"
    assert result.rule_version is None
