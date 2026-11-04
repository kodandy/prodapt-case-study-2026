"""Tests for list_tickets, get_ticket and transition_ticket, written from the B12 tickets spec (section 5)."""

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

import pytest
from ops_ai.scope import Scope
from ops_ai.tickets.routing import Queue, TicketSource
from ops_ai.tickets.service import (
    InvalidTicketTransitionError,
    NewTicket,
    TicketNotFoundError,
    TicketStatus,
    TicketView,
    get_ticket,
    list_tickets,
    open_ticket,
    transition_ticket,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
ASSIGNED_AT = TEST_NOW + timedelta(hours=1)
RESOLVED_AT = TEST_NOW + timedelta(hours=2)
Target = Literal["assigned", "resolved"]


def _clock_at(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


def _open(
    session_factory: sessionmaker[Session],
    key: str,
    *,
    merchant_id: uuid.UUID = MERCHANT,
    source: TicketSource = "dispute",
    at: datetime = TEST_NOW,
) -> uuid.UUID:
    new = NewTicket(
        source=source,
        source_key=key,
        merchant_id=merchant_id,
        title=f"Title {key}",
        reason=f"Reason {key}",
    )
    with session_factory.begin() as session:
        return open_ticket(session, _clock_at(at), new).ticket_id


def _transition(
    session_factory: sessionmaker[Session],
    ticket_id: uuid.UUID,
    target: Target,
    assignee: str | None = None,
    *,
    scope: Scope = SCOPE,
    at: datetime = ASSIGNED_AT,
) -> TicketView:
    with session_factory.begin() as session:
        return transition_ticket(session, _clock_at(at), scope, ticket_id, target, assignee)


def _get(
    session_factory: sessionmaker[Session], ticket_id: uuid.UUID, scope: Scope = SCOPE
) -> TicketView | None:
    with session_factory() as session:
        return get_ticket(session, scope, ticket_id)


def _list(
    session_factory: sessionmaker[Session],
    scope: Scope = SCOPE,
    *,
    status: TicketStatus | None = None,
    queue: Queue | None = None,
    limit: int = 50,
) -> list[TicketView]:
    with session_factory() as session:
        return list_tickets(session, scope, status=status, queue=queue, limit=limit)


def _row(session_factory: sessionmaker[Session], ticket_id: uuid.UUID) -> dict[str, object]:
    with session_factory() as session:
        return dict(
            session.execute(
                text("SELECT * FROM ops_ai.tickets WHERE ticket_id = :ticket_id"), {"ticket_id": ticket_id}
            )
            .mappings()
            .one()
        )


def _assigned_ticket(session_factory: sessionmaker[Session], key: str = "k") -> uuid.UUID:
    ticket_id = _open(session_factory, key)
    _transition(session_factory, ticket_id, "assigned", "alice")
    return ticket_id


def _resolved_ticket(session_factory: sessionmaker[Session], key: str = "k") -> uuid.UUID:
    ticket_id = _assigned_ticket(session_factory, key)
    _transition(session_factory, ticket_id, "resolved", at=RESOLVED_AT)
    return ticket_id


# --- get_ticket -------------------------------------------------------------------------------------------


def test_get_ticket_returns_the_full_view_of_an_open_ticket(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ticket_id = _open(session_factory, "dispute:1", source="dispute")

    # Act
    view = _get(session_factory, ticket_id)

    # Assert
    assert view == TicketView(
        ticket_id=ticket_id,
        source="dispute",
        queue="compliance",
        priority="high",
        status="open",
        title="Title dispute:1",
        reason="Reason dispute:1",
        assignee=None,
        created_at=TEST_NOW,
        updated_at=None,
    )


def test_get_ticket_returns_none_for_a_missing_id(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _open(session_factory, "k")

    # Act
    view = _get(session_factory, uuid.uuid4())

    # Assert
    assert view is None


def test_get_ticket_returns_none_for_another_merchants_ticket(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", merchant_id=OTHER_MERCHANT)

    # Act
    mine = _get(session_factory, ticket_id, SCOPE)
    theirs = _get(session_factory, ticket_id, OTHER_SCOPE)

    # Assert
    assert mine is None
    assert theirs is not None
    assert theirs.ticket_id == ticket_id


# --- list_tickets -----------------------------------------------------------------------------------------


def test_list_tickets_returns_an_empty_list_when_the_merchant_has_none(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _open(session_factory, "theirs", merchant_id=OTHER_MERCHANT)

    # Act
    views = _list(session_factory)

    # Assert
    assert views == []


def test_list_tickets_returns_only_the_scoped_merchants_tickets_newest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    oldest = _open(session_factory, "oldest", at=TEST_NOW)
    newest = _open(session_factory, "newest", at=TEST_NOW + timedelta(hours=2))
    middle = _open(session_factory, "middle", at=TEST_NOW + timedelta(hours=1))
    foreign = _open(session_factory, "foreign", merchant_id=OTHER_MERCHANT, at=TEST_NOW + timedelta(hours=3))

    # Act
    mine = _list(session_factory)
    theirs = _list(session_factory, OTHER_SCOPE)

    # Assert
    assert [view.ticket_id for view in mine] == [newest, middle, oldest]
    assert [view.ticket_id for view in theirs] == [foreign]


def test_list_tickets_breaks_created_at_ties_by_ticket_id(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ids = [_open(session_factory, f"tie-{n}") for n in range(4)]

    # Act
    views = _list(session_factory)

    # Assert
    assert [view.ticket_id for view in views] == sorted(ids)


def test_list_tickets_filters_by_status(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    open_id = _open(session_factory, "open")
    assigned_id = _assigned_ticket(session_factory, "assigned")
    resolved_id = _resolved_ticket(session_factory, "resolved")

    # Act
    opens = _list(session_factory, status="open")
    assigneds = _list(session_factory, status="assigned")
    resolveds = _list(session_factory, status="resolved")

    # Assert
    assert [view.ticket_id for view in opens] == [open_id]
    assert [view.ticket_id for view in assigneds] == [assigned_id]
    assert [view.ticket_id for view in resolveds] == [resolved_id]


def test_list_tickets_filters_by_queue(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    dispute = _open(session_factory, "d", source="dispute")
    unknown = _open(session_factory, "u", source="unknown_payment")
    card = _open(session_factory, "c", source="card_testing")

    # Act
    compliance = _list(session_factory, queue="compliance")
    payments_ops = _list(session_factory, queue="payments_ops")
    fraud = _list(session_factory, queue="fraud")
    settlement = _list(session_factory, queue="settlement")

    # Assert
    assert [view.ticket_id for view in compliance] == [dispute]
    assert [view.ticket_id for view in payments_ops] == [unknown]
    assert [view.ticket_id for view in fraud] == [card]
    assert settlement == []


def test_list_tickets_combines_the_status_and_queue_filters(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _open(session_factory, "d-open", source="dispute")
    assigned_dispute = _open(session_factory, "d-assigned", source="dispute")
    _transition(session_factory, assigned_dispute, "assigned", "alice")
    unknown = _open(session_factory, "u", source="unknown_payment")
    _transition(session_factory, unknown, "assigned", "bob")

    # Act
    views = _list(session_factory, status="assigned", queue="compliance")

    # Assert
    assert [view.ticket_id for view in views] == [assigned_dispute]


def test_list_tickets_honors_the_limit_and_keeps_the_newest(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ids = [_open(session_factory, f"k{n}", at=TEST_NOW + timedelta(minutes=n)) for n in range(5)]

    # Act
    views = _list(session_factory, limit=2)

    # Assert
    assert [view.ticket_id for view in views] == [ids[4], ids[3]]


def test_list_tickets_default_limit_is_50(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        for n in range(51):
            new = NewTicket(
                source="dispute",
                source_key=f"bulk-{n}",
                merchant_id=MERCHANT,
                title="t",
                reason="r",
            )
            open_ticket(session, _clock_at(TEST_NOW + timedelta(seconds=n)), new)

    # Act
    with session_factory() as session:
        views = list_tickets(session, SCOPE)

    # Assert
    assert len(views) == 50


# --- transition_ticket: allowed ---------------------------------------------------------------------------


def test_open_to_assigned_sets_the_assignee_status_and_updated_at(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")

    # Act
    view = _transition(session_factory, ticket_id, "assigned", "alice", at=ASSIGNED_AT)

    # Assert
    expected = TicketView(
        ticket_id=ticket_id,
        source="dispute",
        queue="compliance",
        priority="high",
        status="assigned",
        title="Title k",
        reason="Reason k",
        assignee="alice",
        created_at=TEST_NOW,
        updated_at=ASSIGNED_AT,
    )
    assert view == expected
    assert _get(session_factory, ticket_id) == expected


def test_assigned_to_resolved_keeps_the_assignee_and_bumps_updated_at(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _assigned_ticket(session_factory)

    # Act
    view = _transition(session_factory, ticket_id, "resolved", at=RESOLVED_AT)

    # Assert
    row = _row(session_factory, ticket_id)
    assert (view.status, view.assignee, view.updated_at) == ("resolved", "alice", RESOLVED_AT)
    assert (row["status"], row["assignee"], row["updated_at"]) == ("resolved", "alice", RESOLVED_AT)
    assert row["created_at"] == TEST_NOW


# --- transition_ticket: forbidden -------------------------------------------------------------------------


@pytest.mark.parametrize("assignee", [None, ""], ids=["none", "empty"])
def test_open_to_assigned_without_a_non_empty_assignee_is_rejected_and_the_row_unchanged(
    session_factory: sessionmaker[Session], assignee: str | None
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, "assigned", assignee)
    assert _row(session_factory, ticket_id) == before


def test_open_to_resolved_is_rejected_and_the_row_unchanged(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, "resolved", "alice")
    assert _row(session_factory, ticket_id) == before


def test_assigned_to_assigned_is_rejected_and_keeps_the_original_assignee(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _assigned_ticket(session_factory)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, "assigned", "bob", at=RESOLVED_AT)
    assert _row(session_factory, ticket_id) == before


def test_resolved_to_assigned_is_rejected_and_the_row_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _resolved_ticket(session_factory)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, "assigned", "bob", at=RESOLVED_AT + timedelta(hours=1))
    assert _row(session_factory, ticket_id) == before


def test_resolved_to_resolved_is_rejected_and_the_row_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _resolved_ticket(session_factory)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, "resolved", at=RESOLVED_AT + timedelta(hours=1))
    assert _row(session_factory, ticket_id) == before


def test_a_second_assignment_of_the_same_ticket_is_rejected(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    _transition(session_factory, ticket_id, "assigned", "alice")

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, "assigned", "bob")
    assert _row(session_factory, ticket_id)["assignee"] == "alice"


# --- transition_ticket: not found and isolation -----------------------------------------------------------


def test_transition_of_a_missing_ticket_raises_not_found(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    missing = uuid.uuid4()

    # Act / Assert
    with pytest.raises(TicketNotFoundError):
        _transition(session_factory, missing, "assigned", "alice")


@pytest.mark.parametrize("target", ["assigned", "resolved"])
def test_transition_of_another_merchants_ticket_raises_not_found_and_changes_nothing(
    session_factory: sessionmaker[Session], target: Target
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", merchant_id=OTHER_MERCHANT)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(TicketNotFoundError):
        _transition(session_factory, ticket_id, target, "mallory", scope=SCOPE)
    assert _row(session_factory, ticket_id) == before


def test_transition_of_another_merchants_assigned_ticket_cannot_resolve_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", merchant_id=OTHER_MERCHANT)
    _transition(session_factory, ticket_id, "assigned", "alice", scope=OTHER_SCOPE)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(TicketNotFoundError):
        _transition(session_factory, ticket_id, "resolved", scope=SCOPE, at=RESOLVED_AT)
    assert _row(session_factory, ticket_id) == before


def test_transition_does_not_commit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")

    # Act
    with session_factory() as session:
        transition_ticket(session, _clock_at(ASSIGNED_AT), SCOPE, ticket_id, "assigned", "alice")
        session.rollback()

    # Assert
    row = _row(session_factory, ticket_id)
    assert (row["status"], row["assignee"], row["updated_at"]) == ("open", None, None)
