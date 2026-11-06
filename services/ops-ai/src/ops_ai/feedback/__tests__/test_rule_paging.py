"""Keyset paging of the rule history and audit (docs/specs/r2-agents.md sections 7.1 and 7.2)."""

import base64
import uuid
from datetime import timedelta

import pytest
from ops_ai.feedback.paging import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    CursorKind,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from ops_ai.feedback.thresholds import (
    rule_audit,
    rule_audit_page,
    rule_history,
    rule_history_page,
    thresholds_for_steps,
)
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000011")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000012")
SCOPE = Scope(merchant_id=MERCHANT)

INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
    "(:merchant_id, :version, 1, :payments, :share, :cards, 'revert:v0', NULL, :created_at)"
)
INSERT_ACTOR = text(
    "INSERT INTO ops_ai.fraud_rule_version_actors "
    "(merchant_id, version, actor, actor_role, created_at) VALUES "
    "(:merchant_id, :version, :actor, 'merchant_admin', :created_at)"
)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _insert_versions(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, versions: range, *, actors: bool = True
) -> None:
    payments, share, cards = thresholds_for_steps(1)
    with session_factory.begin() as session:
        for version in versions:
            created_at = TEST_NOW + timedelta(minutes=version)
            session.execute(
                INSERT_VERSION,
                {
                    "merchant_id": merchant_id,
                    "version": version,
                    "payments": payments,
                    "share": share,
                    "cards": cards,
                    "created_at": created_at,
                },
            )
            if actors:
                session.execute(
                    INSERT_ACTOR,
                    {
                        "merchant_id": merchant_id,
                        "version": version,
                        "actor": f"merchant:{merchant_id}",
                        "created_at": created_at,
                    },
                )


# --- cursor codec (7.1) ------------------------------------------------------------------------------


def test_page_size_bounds_are_fifty_by_default_and_two_hundred_at_most() -> None:
    # Arrange / Act / Assert
    assert (DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE) == (50, 200)


@pytest.mark.parametrize("kind", ["history", "audit"])
@pytest.mark.parametrize("version", [1, 7, 12345])
def test_a_cursor_round_trips_to_its_version_for_its_kind_and_merchant(kind: CursorKind, version: int) -> None:
    # Arrange
    cursor = encode_cursor(kind, MERCHANT, version)

    # Act
    decoded = decode_cursor(cursor, kind, MERCHANT)

    # Assert
    assert decoded == version
    assert cursor == _b64(f"{kind}:{MERCHANT}:{version}".encode())


def test_a_cursor_issued_to_another_merchant_is_rejected() -> None:
    # Arrange
    cursor = encode_cursor("history", OTHER_MERCHANT, 5)

    # Act / Assert
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "history", MERCHANT)


def test_a_history_cursor_is_rejected_by_the_audit_kind() -> None:
    # Arrange
    cursor = encode_cursor("history", MERCHANT, 5)

    # Act / Assert
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "audit", MERCHANT)


@pytest.mark.parametrize(
    "cursor",
    [
        "",
        "not a cursor",
        "!!!!",
        "%%%",
        _b64(f"history:{MERCHANT}:5".encode()) + "==",
        _b64(f"history:{MERCHANT}:5".encode()) + "=",
        _b64(f"history:{MERCHANT}:0".encode()),
        _b64(f"history:{MERCHANT}:-1".encode()),
        _b64(f"history:{MERCHANT}:+5".encode()),
        _b64(f"history:{MERCHANT}:05".encode()),
        _b64(f"history:{MERCHANT}: 5".encode()),
        _b64(f"history:{MERCHANT}:5.0".encode()),
        _b64(f"history:{MERCHANT}:five".encode()),
        _b64(f"history:{MERCHANT}:".encode()),
        _b64(f"history:{MERCHANT}:5:6".encode()),
        _b64(f"history:{str(MERCHANT).upper()}:5".encode()),
        _b64(f"history:{MERCHANT.hex}:5".encode()),
        _b64(b"history:not-a-uuid:5"),
        _b64(f"HISTORY:{MERCHANT}:5".encode()),
        _b64(f"history:{MERCHANT}:٥".encode()),
        _b64(b"\xff\xfe\xfd"),
        _b64(f"history:{MERCHANT}:{'9' * 40}".encode()),
    ],
)
def test_garbage_or_tampered_cursors_are_rejected(cursor: str) -> None:
    # Arrange: each value is not what encode_cursor returns for ("history", MERCHANT, version >= 1)

    # Act / Assert
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, "history", MERCHANT)


# --- page queries (7.2) ------------------------------------------------------------------------------


def test_history_pages_walk_every_version_once_newest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_versions(session_factory, MERCHANT, range(1, 8))
    _insert_versions(session_factory, OTHER_MERCHANT, range(1, 20))

    # Act
    with session_factory() as session:
        first = rule_history_page(session, SCOPE, 3, None)
        second = rule_history_page(session, SCOPE, 3, first.next_before)
        third = rule_history_page(session, SCOPE, 3, second.next_before)
        everything = rule_history(session, SCOPE)

    # Assert
    assert [rule.version for rule in first.items] == [7, 6, 5]
    assert first.next_before == 5
    assert [rule.version for rule in second.items] == [4, 3, 2]
    assert second.next_before == 2
    assert [rule.version for rule in third.items] == [1]
    assert third.next_before is None
    assert first.items + second.items + third.items == everything


def test_a_history_page_that_exactly_fills_the_limit_is_the_last_page(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_versions(session_factory, MERCHANT, range(1, 4))

    # Act
    with session_factory() as session:
        page = rule_history_page(session, SCOPE, 3, None)

    # Assert
    assert [rule.version for rule in page.items] == [3, 2, 1]
    assert page.next_before is None


def test_a_history_page_never_returns_another_merchants_versions(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_versions(session_factory, OTHER_MERCHANT, range(1, 6))

    # Act
    with session_factory() as session:
        first = rule_history_page(session, SCOPE, 10, None)
        below = rule_history_page(session, SCOPE, 10, 4)

    # Assert
    assert first.items == []
    assert first.next_before is None
    assert below.items == []
    assert below.next_before is None


def test_versions_appended_between_pages_do_not_shift_the_next_page(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_versions(session_factory, MERCHANT, range(1, 6))
    with session_factory() as session:
        first = rule_history_page(session, SCOPE, 2, None)
    _insert_versions(session_factory, MERCHANT, range(6, 9))

    # Act
    with session_factory() as session:
        second = rule_history_page(session, SCOPE, 2, first.next_before)

    # Assert
    assert [rule.version for rule in first.items] == [5, 4]
    assert [rule.version for rule in second.items] == [3, 2]
    assert second.next_before == 2


def test_audit_pages_walk_every_entry_once_newest_first_including_legacy_actorless_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_versions(session_factory, MERCHANT, range(1, 3), actors=False)
    _insert_versions(session_factory, MERCHANT, range(3, 6))
    _insert_versions(session_factory, OTHER_MERCHANT, range(1, 9))

    # Act
    with session_factory() as session:
        first = rule_audit_page(session, SCOPE, 2, None)
        second = rule_audit_page(session, SCOPE, 2, first.next_before)
        third = rule_audit_page(session, SCOPE, 2, second.next_before)
        everything = rule_audit(session, SCOPE)

    # Assert
    assert [entry.version for entry in first.items] == [5, 4]
    assert [entry.version for entry in second.items] == [3, 2]
    assert [entry.version for entry in third.items] == [1]
    assert (first.next_before, second.next_before, third.next_before) == (4, 2, None)
    assert [entry.actor for entry in second.items] == [f"merchant:{MERCHANT}", None]
    assert first.items + second.items + third.items == everything
