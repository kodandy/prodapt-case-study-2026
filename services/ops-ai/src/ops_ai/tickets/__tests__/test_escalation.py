"""Tests for wants_human and escalate_assistant_turn, written from the B12 tickets spec (section 4a)."""

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from ops_ai.assistant.service import AnswerMode
from ops_ai.scope import Scope
from ops_ai.tickets.escalation import escalate_assistant_turn, wants_human
from ops_ai.tickets.routing import Queue
from ops_ai.tickets.service import TicketOpened
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
HUMAN_QUESTION = "Can I talk to a human?"
PLAIN_QUESTION = "declined payments"
HUMAN_REASON_PREFIX = "Merchant asked for a human: "
FALLBACK_REASON_PREFIX = "Assistant could not answer (LLM unavailable): "


def _clock_at(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _escalate(
    session_factory: sessionmaker[Session],
    question: str,
    mode: AnswerMode,
    *,
    merchant_id: uuid.UUID = MERCHANT,
    at: datetime = TEST_NOW,
) -> TicketOpened | None:
    with session_factory.begin() as session:
        return escalate_assistant_turn(session, _clock_at(at), Scope(merchant_id=merchant_id), question, mode)


def _tickets(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(
                text("SELECT * FROM ops_ai.tickets ORDER BY created_at, source_key")
            ).mappings()
        ]


def _expected_key(merchant_id: uuid.UUID, day: str, question: str) -> str:
    normalized = " ".join(question.split()).lower()
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    return f"assistant_escalation:{merchant_id}:{day}:{digest}"


@pytest.mark.parametrize(
    "question",
    [
        "Can I talk to a human?",
        "I want a real person",
        "let me speak to a person",
        "connect me to an agent",
        "I need someone to look at this",
        "please escalate this",
        "contact the support team",
        "get me an operator",
        "I want a representative",
        "HUMAN",
        "Please ESCALATE",
        "human",
    ],
)
def test_wants_human_is_true_for_a_request_for_a_person(question: str) -> None:
    # Arrange / Act
    result = wants_human(question)

    # Assert
    assert result is True


@pytest.mark.parametrize(
    "question",
    [
        "declined payments",
        "humanity",
        "Why did settlement drop?",
        "the agents of change",
        "escalated",
        "personal data",
        "",
    ],
    ids=[
        "declined_payments",
        "humanity_is_not_human",
        "settlement",
        "agents_plural",
        "escalated",
        "personal",
        "empty",
    ],
)
def test_wants_human_is_false_without_a_whole_word_match(question: str) -> None:
    # Arrange / Act
    result = wants_human(question)

    # Assert
    assert result is False


def test_escalate_in_t2_fallback_mode_opens_a_ticket_with_the_unavailable_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    question = PLAIN_QUESTION

    # Act
    opened = _escalate(session_factory, question, "t2_fallback")

    # Assert
    (row,) = _tickets(session_factory)
    assert opened is not None
    assert opened.created is True
    assert row == {
        "ticket_id": opened.ticket_id,
        "source_key": _expected_key(MERCHANT, "2026-11-03", question),
        "source": "assistant_escalation",
        "queue": "payments_ops",
        "priority": "normal",
        "status": "open",
        "assignee": None,
        "merchant_id": MERCHANT,
        "title": "Assistant escalation",
        "reason": f"{FALLBACK_REASON_PREFIX}declined payments",
        "alert_id": None,
        "created_at": TEST_NOW,
        "updated_at": None,
    }


def test_escalate_when_the_merchant_asks_for_a_human_opens_a_ticket_with_the_human_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    question = HUMAN_QUESTION

    # Act
    opened = _escalate(session_factory, question, "llm")

    # Assert
    (row,) = _tickets(session_factory)
    assert opened is not None
    assert opened.created is True
    assert row["ticket_id"] == opened.ticket_id
    assert row["reason"] == "Merchant asked for a human: Can I talk to a human?"
    assert row["source"] == "assistant_escalation"
    assert row["source_key"] == _expected_key(MERCHANT, "2026-11-03", question)


def test_escalate_prefers_the_human_reason_when_the_llm_is_down_and_a_human_was_requested(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    question = HUMAN_QUESTION

    # Act
    opened = _escalate(session_factory, question, "t2_fallback")

    # Assert
    (row,) = _tickets(session_factory)
    assert opened is not None
    assert row["reason"] == f"{HUMAN_REASON_PREFIX}{question}"


@pytest.mark.parametrize(
    "question",
    [PLAIN_QUESTION, "humanity", "why was my payout short?"],
    ids=["declined_payments", "humanity", "payout"],
)
def test_escalate_in_llm_mode_without_a_human_request_returns_none_and_writes_nothing(
    session_factory: sessionmaker[Session], question: str
) -> None:
    # Arrange / Act
    opened = _escalate(session_factory, question, "llm")

    # Assert
    assert opened is None
    assert _tickets(session_factory) == []


@pytest.mark.parametrize(
    ("question", "mode", "queue"),
    [
        ("my card was stolen, I want a human", "llm", "fraud"),
        ("where is my payout", "t2_fallback", "settlement"),
        ("explain the chargeback, escalate please", "llm", "compliance"),
        ("why are payments declining", "t2_fallback", "payments_ops"),
        ("feedback, talk to someone", "llm", "payments_ops"),
    ],
    ids=["fraud", "settlement", "compliance", "payments", "feedback_is_not_fee"],
)
def test_escalate_routes_the_ticket_by_the_classified_topic(
    session_factory: sessionmaker[Session], question: str, mode: AnswerMode, queue: Queue
) -> None:
    # Arrange / Act
    opened = _escalate(session_factory, question, mode)

    # Assert
    (row,) = _tickets(session_factory)
    assert opened is not None
    assert (row["queue"], row["priority"]) == (queue, "normal")


def test_escalate_collapses_whitespace_in_the_stored_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    question = "  Can I   talk\nto a\t human?  "

    # Act
    _escalate(session_factory, question, "llm")

    # Assert
    (row,) = _tickets(session_factory)
    assert row["reason"] == "Merchant asked for a human: Can I talk to a human?"


def test_escalate_truncates_the_question_in_the_reason_to_300_characters(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    question = "human " + "x" * 500

    # Act
    _escalate(session_factory, question, "llm")

    # Assert
    (row,) = _tickets(session_factory)
    assert row["reason"] == HUMAN_REASON_PREFIX + question[:300]
    assert len(str(row["reason"])) == len(HUMAN_REASON_PREFIX) + 300


def test_escalate_keeps_a_question_of_exactly_300_characters_whole(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    question = "human " + "y" * 294

    # Act
    _escalate(session_factory, question, "llm")

    # Assert
    (row,) = _tickets(session_factory)
    assert len(question) == 300
    assert row["reason"] == HUMAN_REASON_PREFIX + question


def test_the_same_question_from_the_same_merchant_on_the_same_day_reuses_the_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _escalate(session_factory, HUMAN_QUESTION, "llm")

    # Act
    second = _escalate(session_factory, HUMAN_QUESTION, "t2_fallback", at=TEST_NOW + timedelta(hours=5))

    # Assert
    assert first is not None
    assert second == TicketOpened(ticket_id=first.ticket_id, created=False)
    (row,) = _tickets(session_factory)
    assert row["ticket_id"] == first.ticket_id
    assert row["created_at"] == TEST_NOW


def test_the_dedupe_key_ignores_case_and_whitespace_differences(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _escalate(session_factory, "Can I talk to a human?", "llm")

    # Act
    second = _escalate(session_factory, "  CAN  I talk   TO a human?", "llm")

    # Assert
    assert first is not None
    assert second == TicketOpened(ticket_id=first.ticket_id, created=False)
    assert len(_tickets(session_factory)) == 1


def test_the_same_question_from_another_merchant_opens_a_separate_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _escalate(session_factory, HUMAN_QUESTION, "llm")

    # Act
    second = _escalate(session_factory, HUMAN_QUESTION, "llm", merchant_id=OTHER_MERCHANT)

    # Assert
    assert first is not None
    assert second is not None
    assert second.created is True
    assert second.ticket_id != first.ticket_id
    rows = _tickets(session_factory)
    assert sorted(str(row["merchant_id"]) for row in rows) == sorted([str(MERCHANT), str(OTHER_MERCHANT)])


def test_the_same_question_on_the_next_utc_day_opens_a_new_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _escalate(session_factory, HUMAN_QUESTION, "llm")

    # Act
    second = _escalate(session_factory, HUMAN_QUESTION, "llm", at=TEST_NOW + timedelta(days=1))

    # Assert
    assert first is not None
    assert second is not None
    assert second.created is True
    assert second.ticket_id != first.ticket_id
    assert [row["source_key"] for row in _tickets(session_factory)] == [
        _expected_key(MERCHANT, "2026-11-03", HUMAN_QUESTION),
        _expected_key(MERCHANT, "2026-11-04", HUMAN_QUESTION),
    ]


def test_the_day_in_the_key_rolls_over_at_utc_midnight(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    before_midnight = TEST_NOW.replace(hour=23, minute=59, second=59)
    midnight = before_midnight + timedelta(seconds=1)
    first = _escalate(session_factory, HUMAN_QUESTION, "llm", at=before_midnight)

    # Act
    same_day = _escalate(session_factory, HUMAN_QUESTION, "llm", at=before_midnight)
    next_day = _escalate(session_factory, HUMAN_QUESTION, "llm", at=midnight)

    # Assert
    assert first is not None
    assert same_day == TicketOpened(ticket_id=first.ticket_id, created=False)
    assert next_day is not None
    assert next_day.created is True
    assert len(_tickets(session_factory)) == 2


def test_a_different_question_on_the_same_day_opens_a_new_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _escalate(session_factory, HUMAN_QUESTION, "llm")

    # Act
    second = _escalate(session_factory, "I want a real person", "llm")

    # Assert
    assert first is not None
    assert second is not None
    assert second.created is True
    assert second.ticket_id != first.ticket_id
    assert len(_tickets(session_factory)) == 2


def test_escalate_does_not_commit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        scope = Scope(merchant_id=MERCHANT)
        escalate_assistant_turn(session, _clock_at(TEST_NOW), scope, HUMAN_QUESTION, "llm")
        session.rollback()

    # Assert
    assert _tickets(session_factory) == []
