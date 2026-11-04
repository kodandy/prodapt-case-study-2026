"""Tests for open_ticket and the tickets table additions, from the B12 tickets spec (sections 2, 3)."""

import uuid
from datetime import datetime, timedelta

import pytest
from ops_ai.tickets.routing import EscalationTopic, Priority, Queue, TicketSource
from ops_ai.tickets.service import NewTicket, TicketOpened, open_ticket
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
LATER = TEST_NOW + timedelta(hours=3)


def _new(
    source_key: str = "key-1",
    *,
    source: TicketSource = "dispute",
    topic: EscalationTopic | None = None,
    title: str = "A title",
    reason: str = "A reason",
    merchant_id: uuid.UUID = MERCHANT,
) -> NewTicket:
    return NewTicket(
        source=source,
        source_key=source_key,
        merchant_id=merchant_id,
        title=title,
        reason=reason,
        topic=topic,
    )


def _open(session_factory: sessionmaker[Session], new: NewTicket, at: datetime = TEST_NOW) -> TicketOpened:
    with session_factory.begin() as session:
        return open_ticket(session, lambda: at, new)


def _rows(session_factory: sessionmaker[Session], query: str = "") -> list[dict[str, object]]:
    sql = query or "SELECT * FROM ops_ai.tickets ORDER BY created_at, ticket_id"
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(sql)).mappings()]


def test_open_ticket_inserts_one_open_unassigned_row_with_the_given_fields(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    new = _new("dispute:abc", source="dispute", title="Chargeback received", reason="Because of reasons")

    # Act
    opened = _open(session_factory, new)

    # Assert
    (row,) = _rows(session_factory)
    assert opened.created is True
    assert row == {
        "ticket_id": opened.ticket_id,
        "source_key": "dispute:abc",
        "source": "dispute",
        "queue": "compliance",
        "priority": "high",
        "status": "open",
        "assignee": None,
        "merchant_id": MERCHANT,
        "title": "Chargeback received",
        "reason": "Because of reasons",
        "alert_id": None,
        "created_at": TEST_NOW,
        "updated_at": None,
    }


@pytest.mark.parametrize(
    ("source", "topic", "queue", "priority"),
    [
        ("card_testing", None, "fraud", "high"),
        ("dispute", None, "compliance", "high"),
        ("unknown_payment", None, "payments_ops", "normal"),
        ("assistant_escalation", None, "payments_ops", "normal"),
        ("assistant_escalation", "fraud", "fraud", "normal"),
        ("assistant_escalation", "settlement", "settlement", "normal"),
        ("assistant_escalation", "compliance", "compliance", "normal"),
        ("assistant_escalation", "payments", "payments_ops", "normal"),
    ],
    ids=[
        "card_testing",
        "dispute",
        "unknown_payment",
        "escalation_no_topic",
        "escalation_fraud",
        "escalation_settlement",
        "escalation_compliance",
        "escalation_payments",
    ],
)
def test_open_ticket_takes_queue_and_priority_from_the_routing_table(
    session_factory: sessionmaker[Session],
    source: TicketSource,
    topic: EscalationTopic | None,
    queue: Queue,
    priority: Priority,
) -> None:
    # Arrange
    new = _new("routing-key", source=source, topic=topic)

    # Act
    _open(session_factory, new)

    # Assert
    (row,) = _rows(session_factory)
    assert (row["source"], row["queue"], row["priority"]) == (source, queue, priority)


def test_open_ticket_stamps_created_at_from_the_injected_clock(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    new = _new()

    # Act
    _open(session_factory, new, at=LATER)

    # Assert
    (row,) = _rows(session_factory)
    assert row["created_at"] == LATER


def test_open_ticket_with_an_existing_source_key_creates_nothing_and_returns_the_existing_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _open(session_factory, _new("dup-key", title="First title", reason="First reason"))

    # Act
    second = _open(session_factory, _new("dup-key", title="Second title", reason="Second reason"), at=LATER)

    # Assert
    assert first.created is True
    assert second == TicketOpened(ticket_id=first.ticket_id, created=False)
    assert len(_rows(session_factory)) == 1


def test_open_ticket_conflict_leaves_the_existing_row_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _open(session_factory, _new("dup-key", source="dispute", title="First title", reason="First reason"))
    (before,) = _rows(session_factory)

    # Act
    _open(
        session_factory,
        _new(
            "dup-key",
            source="assistant_escalation",
            topic="fraud",
            title="Second title",
            reason="Second reason",
            merchant_id=uuid.uuid4(),
        ),
        at=LATER,
    )

    # Assert
    (after,) = _rows(session_factory)
    assert after == before


def test_open_ticket_conflict_does_not_reset_a_ticket_that_is_already_assigned(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _open(session_factory, _new("dup-key"))
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE ops_ai.tickets SET status = 'assigned', assignee = 'alice', updated_at = :at "
                "WHERE ticket_id = :ticket_id"
            ),
            {"at": LATER, "ticket_id": first.ticket_id},
        )

    # Act
    second = _open(session_factory, _new("dup-key"), at=LATER + timedelta(hours=1))

    # Assert
    (row,) = _rows(session_factory)
    assert second == TicketOpened(ticket_id=first.ticket_id, created=False)
    assert (row["status"], row["assignee"], row["updated_at"]) == ("assigned", "alice", LATER)


def test_open_ticket_with_different_source_keys_creates_separate_tickets(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    first = _open(session_factory, _new("key-a"))
    second = _open(session_factory, _new("key-b"))

    # Assert
    assert (first.created, second.created) == (True, True)
    assert first.ticket_id != second.ticket_id
    assert sorted(str(row["source_key"]) for row in _rows(session_factory)) == ["key-a", "key-b"]


def test_open_ticket_does_not_commit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory() as session:
        # Act
        open_ticket(session, lambda: TEST_NOW, _new("uncommitted"))
        session.rollback()

    # Assert
    assert _rows(session_factory) == []


def test_tickets_table_defaults_source_to_card_testing_when_the_column_is_omitted(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = uuid.uuid4()
    with session_factory.begin() as session:
        # Act
        session.execute(
            text(
                "INSERT INTO ops_ai.tickets "
                "(ticket_id, source_key, queue, priority, title, reason, created_at) "
                "VALUES (:ticket_id, 'legacy', 'fraud', 'high', 't', 'r', :at)"
            ),
            {"ticket_id": ticket_id, "at": TEST_NOW},
        )

    # Assert
    (row,) = _rows(session_factory)
    assert (row["source"], row["status"], row["assignee"], row["updated_at"]) == (
        "card_testing",
        "open",
        None,
        None,
    )


def test_tickets_table_rejects_an_unknown_source(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    insert = text(
        "INSERT INTO ops_ai.tickets "
        "(ticket_id, source_key, source, queue, priority, title, reason, created_at) "
        "VALUES (:ticket_id, 'bogus', 'carrier_pigeon', 'fraud', 'high', 't', 'r', :at)"
    )

    # Act / Assert
    with pytest.raises(IntegrityError, match="ck_tickets_source"), session_factory.begin() as session:
        session.execute(insert, {"ticket_id": uuid.uuid4(), "at": TEST_NOW})
    assert _rows(session_factory) == []


def test_tickets_table_has_the_source_assignee_and_updated_at_columns(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    query = (
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'ops_ai' AND table_name = 'tickets' "
        "AND column_name IN ('source', 'assignee', 'updated_at') ORDER BY column_name"
    )

    # Act
    rows = _rows(session_factory, query)

    # Assert
    assert rows == [
        {
            "column_name": "assignee",
            "data_type": "character varying",
            "character_maximum_length": 64,
            "is_nullable": "YES",
        },
        {
            "column_name": "source",
            "data_type": "character varying",
            "character_maximum_length": 32,
            "is_nullable": "NO",
        },
        {
            "column_name": "updated_at",
            "data_type": "timestamp with time zone",
            "character_maximum_length": None,
            "is_nullable": "YES",
        },
    ]


def test_tickets_table_check_constraint_is_named_ck_tickets_source(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    query = "SELECT conname FROM pg_constraint WHERE conname = 'ck_tickets_source'"

    # Act
    rows = _rows(session_factory, query)

    # Assert
    assert rows == [{"conname": "ck_tickets_source"}]
