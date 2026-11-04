"""Long-term merchant memory store, written from docs/specs/memory.md section B10.3 (against Postgres)."""

import uuid
from datetime import datetime, timedelta

import pytest
from ops_ai.memory.session import PendingMemory, owner_for
from ops_ai.memory.store import (
    RECALL_LIMIT,
    MemoryRecord,
    correct_memory,
    delete_memory,
    recall_memories,
    save_confirmed_memory,
)
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

SELECT_ROW = text(
    "SELECT merchant_id, created_by, content, source_session_id, source_turn_id, supersedes_id, "
    "created_at, superseded_at, deleted_at, deleted_by FROM ops_ai.merchant_memory WHERE memory_id = :id"
)
INSERT_ROW = text(
    "INSERT INTO ops_ai.merchant_memory (memory_id, merchant_id, created_by, content, source_session_id, "
    "source_turn_id, created_at) "
    "VALUES (:id, :merchant_id, 'merchant:x', :content, :session_id, :turn_id, :at)"
)


def _scope() -> Scope:
    return Scope(merchant_id=uuid.uuid4())


def _proposal(content: str = "refunds go through acq_b") -> PendingMemory:
    return PendingMemory(proposal_id=uuid.uuid4(), content=content, source_turn_id=uuid.uuid4())


def _save(
    session_factory: sessionmaker[Session],
    scope: Scope,
    content: str,
    now: datetime = TEST_NOW,
    session_id: uuid.UUID | None = None,
) -> MemoryRecord:
    with session_factory() as db:
        return save_confirmed_memory(
            db, scope, owner_for(scope), session_id or uuid.uuid4(), _proposal(content), now
        )


def _recall(
    session_factory: sessionmaker[Session], scope: Scope, limit: int | None = None
) -> list[MemoryRecord]:
    with session_factory() as db:
        if limit is None:
            return recall_memories(db, scope)
        return recall_memories(db, scope, limit=limit)


def _correct(
    session_factory: sessionmaker[Session],
    scope: Scope,
    memory_id: uuid.UUID,
    content: str,
    now: datetime = TEST_NOW,
) -> MemoryRecord | None:
    with session_factory() as db:
        return correct_memory(db, scope, owner_for(scope), memory_id, content, now)


def _delete(
    session_factory: sessionmaker[Session], scope: Scope, memory_id: uuid.UUID, now: datetime = TEST_NOW
) -> bool:
    with session_factory() as db:
        return delete_memory(db, scope, owner_for(scope), memory_id, now)


def _row(session_factory: sessionmaker[Session], memory_id: uuid.UUID) -> dict[str, object]:
    with session_factory() as db:
        found = db.execute(SELECT_ROW, {"id": memory_id}).mappings().one()
        return dict(found)


def _row_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as db:
        return int(db.execute(text("SELECT count(*) FROM ops_ai.merchant_memory")).scalar_one())


def _contents(records: list[MemoryRecord]) -> list[str]:
    return [record.content for record in records]


# --- save_confirmed_memory -------------------------------------------------------------------------------


def test_save_confirmed_memory_returns_a_record_carrying_the_source_ids(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    session_id = uuid.uuid4()
    proposal = _proposal("refunds go through acq_b")

    # Act
    with session_factory() as db:
        record = save_confirmed_memory(db, scope, owner_for(scope), session_id, proposal, TEST_NOW)

    # Assert
    assert record == MemoryRecord(
        memory_id=record.memory_id,
        content="refunds go through acq_b",
        source_session_id=session_id,
        source_turn_id=proposal.source_turn_id,
        supersedes_id=None,
        created_at=TEST_NOW,
    )


def test_save_confirmed_memory_commits_an_active_row_owned_by_the_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    owner = owner_for(scope)
    session_id = uuid.uuid4()
    proposal = _proposal("refunds go through acq_b")

    # Act
    with session_factory() as db:
        record = save_confirmed_memory(db, scope, owner, session_id, proposal, TEST_NOW)

    # Assert
    assert _row(session_factory, record.memory_id) == {
        "merchant_id": scope.merchant_id,
        "created_by": owner.user_id,
        "content": "refunds go through acq_b",
        "source_session_id": session_id,
        "source_turn_id": proposal.source_turn_id,
        "supersedes_id": None,
        "created_at": TEST_NOW,
        "superseded_at": None,
        "deleted_at": None,
        "deleted_by": None,
    }


def test_save_confirmed_memory_accepts_content_of_exactly_500_characters(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    content = "x" * 500

    # Act
    record = _save(session_factory, scope, content)

    # Assert
    assert record.content == content
    assert _row_count(session_factory) == 1


# --- recall_memories -------------------------------------------------------------------------------------


def test_recall_memories_returns_the_newest_first(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    for index, content in enumerate(["oldest", "middle", "newest"]):
        _save(session_factory, scope, content, now=TEST_NOW + timedelta(minutes=index))

    # Act
    recalled = _recall(session_factory, scope)

    # Assert
    assert _contents(recalled) == ["newest", "middle", "oldest"]


def test_recall_memories_returns_full_records(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    session_id = uuid.uuid4()
    saved = _save(session_factory, scope, "refunds go through acq_b", session_id=session_id)

    # Act
    recalled = _recall(session_factory, scope)

    # Assert
    assert recalled == [saved]


def test_recall_memories_returns_only_the_scope_merchants_memories(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope_a, scope_b = _scope(), _scope()
    _save(session_factory, scope_a, "memory of A")
    _save(session_factory, scope_b, "memory of B")

    # Act
    recalled_a = _recall(session_factory, scope_a)
    recalled_b = _recall(session_factory, scope_b)

    # Assert
    assert _contents(recalled_a) == ["memory of A"]
    assert _contents(recalled_b) == ["memory of B"]


def test_recall_memories_for_a_merchant_without_memories_is_empty(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _save(session_factory, _scope(), "someone else's memory")

    # Act
    recalled = _recall(session_factory, _scope())

    # Assert
    assert recalled == []


def test_recall_memories_excludes_deleted_and_superseded_rows(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    kept = _save(session_factory, scope, "kept", now=TEST_NOW)
    deleted = _save(session_factory, scope, "deleted", now=TEST_NOW + timedelta(minutes=1))
    superseded = _save(session_factory, scope, "superseded", now=TEST_NOW + timedelta(minutes=2))
    _delete(session_factory, scope, deleted.memory_id, TEST_NOW + timedelta(minutes=3))
    replacement = _correct(
        session_factory, scope, superseded.memory_id, "replacement", TEST_NOW + timedelta(minutes=4)
    )

    # Act
    recalled = _recall(session_factory, scope)

    # Assert
    assert replacement is not None
    assert [record.memory_id for record in recalled] == [replacement.memory_id, kept.memory_id]


def test_recall_memories_honours_the_limit_keeping_the_newest(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    for index in range(4):
        _save(session_factory, scope, f"memory {index}", now=TEST_NOW + timedelta(minutes=index))

    # Act
    recalled = _recall(session_factory, scope, limit=2)

    # Assert
    assert _contents(recalled) == ["memory 3", "memory 2"]


def test_recall_memories_defaults_to_the_recall_limit(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    total = RECALL_LIMIT + 1
    for index in range(total):
        _save(session_factory, scope, f"memory {index}", now=TEST_NOW + timedelta(minutes=index))

    # Act
    recalled = _recall(session_factory, scope)

    # Assert
    assert RECALL_LIMIT == 20
    assert len(recalled) == RECALL_LIMIT
    assert recalled[0].content == f"memory {total - 1}"
    assert recalled[-1].content == "memory 1"


# --- correct_memory --------------------------------------------------------------------------------------


def test_correct_memory_creates_a_new_row_pointing_at_the_old_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    original = _save(session_factory, scope, "old content")
    correction_time = TEST_NOW + timedelta(hours=1)

    # Act
    corrected = _correct(session_factory, scope, original.memory_id, "new content", correction_time)

    # Assert
    assert corrected is not None
    assert corrected.memory_id != original.memory_id
    assert (corrected.content, corrected.supersedes_id, corrected.created_at) == (
        "new content",
        original.memory_id,
        correction_time,
    )
    new_row = _row(session_factory, corrected.memory_id)
    assert (new_row["supersedes_id"], new_row["superseded_at"], new_row["deleted_at"]) == (
        original.memory_id,
        None,
        None,
    )


def test_correct_memory_marks_the_old_row_superseded_without_deleting_it(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    original = _save(session_factory, scope, "old content")
    correction_time = TEST_NOW + timedelta(hours=1)

    # Act
    _correct(session_factory, scope, original.memory_id, "new content", correction_time)

    # Assert
    old_row = _row(session_factory, original.memory_id)
    assert (old_row["content"], old_row["superseded_at"], old_row["deleted_at"]) == (
        "old content",
        correction_time,
        None,
    )
    assert _row_count(session_factory) == 2


def test_correct_memory_keeps_the_source_session_and_turn_ids(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    session_id = uuid.uuid4()
    original = _save(session_factory, scope, "old content", session_id=session_id)

    # Act
    corrected = _correct(session_factory, scope, original.memory_id, "new content")

    # Assert
    assert corrected is not None
    assert (corrected.source_session_id, corrected.source_turn_id) == (
        session_id,
        original.source_turn_id,
    )
    new_row = _row(session_factory, corrected.memory_id)
    assert (new_row["source_session_id"], new_row["source_turn_id"]) == (session_id, original.source_turn_id)


def test_correct_memory_records_the_correcting_user_as_creator(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    original = _save(session_factory, scope, "old content")

    # Act
    corrected = _correct(session_factory, scope, original.memory_id, "new content")

    # Assert
    assert corrected is not None
    assert _row(session_factory, corrected.memory_id)["created_by"] == owner_for(scope).user_id


def test_correct_memory_of_an_unknown_id_returns_none_and_writes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    _save(session_factory, scope, "existing")

    # Act
    corrected = _correct(session_factory, scope, uuid.uuid4(), "new content")

    # Assert
    assert corrected is None
    assert _row_count(session_factory) == 1


def test_correct_memory_of_another_merchants_memory_returns_none_and_leaves_the_row_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope_a, scope_b = _scope(), _scope()
    memory_of_a = _save(session_factory, scope_a, "A's memory")
    before = _row(session_factory, memory_of_a.memory_id)

    # Act
    corrected = _correct(session_factory, scope_b, memory_of_a.memory_id, "B's overwrite")

    # Assert
    assert corrected is None
    assert _row(session_factory, memory_of_a.memory_id) == before
    assert _row_count(session_factory) == 1
    assert _contents(_recall(session_factory, scope_a)) == ["A's memory"]


def test_correct_memory_of_a_deleted_memory_returns_none(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    memory = _save(session_factory, scope, "to be deleted")
    _delete(session_factory, scope, memory.memory_id)

    # Act
    corrected = _correct(session_factory, scope, memory.memory_id, "revived")

    # Assert
    assert corrected is None
    assert _row_count(session_factory) == 1


def test_correct_memory_of_an_already_superseded_memory_returns_none(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    memory = _save(session_factory, scope, "first version")
    _correct(session_factory, scope, memory.memory_id, "second version")

    # Act
    corrected = _correct(session_factory, scope, memory.memory_id, "forked version")

    # Assert
    assert corrected is None
    assert _row_count(session_factory) == 2


# --- delete_memory ---------------------------------------------------------------------------------------


def test_delete_memory_soft_deletes_the_row_and_hides_it_from_recall(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope = _scope()
    owner = owner_for(scope)
    memory = _save(session_factory, scope, "to be deleted")
    deletion_time = TEST_NOW + timedelta(hours=2)

    # Act
    deleted = _delete(session_factory, scope, memory.memory_id, deletion_time)

    # Assert
    assert deleted is True
    row = _row(session_factory, memory.memory_id)
    assert (row["deleted_at"], row["deleted_by"], row["superseded_at"]) == (
        deletion_time,
        owner.user_id,
        None,
    )
    assert _row_count(session_factory) == 1
    assert _recall(session_factory, scope) == []


def test_delete_memory_twice_returns_false_the_second_time(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    memory = _save(session_factory, scope, "to be deleted")
    first_deletion = TEST_NOW + timedelta(hours=1)
    _delete(session_factory, scope, memory.memory_id, first_deletion)

    # Act
    second = _delete(session_factory, scope, memory.memory_id, TEST_NOW + timedelta(hours=2))

    # Assert
    assert second is False
    assert _row(session_factory, memory.memory_id)["deleted_at"] == first_deletion


def test_delete_memory_of_another_merchants_memory_returns_false_and_leaves_the_row_unchanged(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    scope_a, scope_b = _scope(), _scope()
    memory_of_a = _save(session_factory, scope_a, "A's memory")
    before = _row(session_factory, memory_of_a.memory_id)

    # Act
    deleted = _delete(session_factory, scope_b, memory_of_a.memory_id)

    # Assert
    assert deleted is False
    assert _row(session_factory, memory_of_a.memory_id) == before
    assert _contents(_recall(session_factory, scope_a)) == ["A's memory"]


def test_delete_memory_of_an_unknown_id_returns_false(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()

    # Act
    deleted = _delete(session_factory, scope, uuid.uuid4())

    # Assert
    assert deleted is False


def test_delete_memory_of_a_superseded_memory_returns_false(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = _scope()
    memory = _save(session_factory, scope, "first version")
    _correct(session_factory, scope, memory.memory_id, "second version")

    # Act
    deleted = _delete(session_factory, scope, memory.memory_id)

    # Assert
    assert deleted is False
    assert _row(session_factory, memory.memory_id)["deleted_at"] is None


# --- table constraints -----------------------------------------------------------------------------------


@pytest.mark.parametrize("content", ["", "x" * 501], ids=["empty", "501_chars"])
def test_the_table_rejects_content_outside_one_to_500_characters(
    session_factory: sessionmaker[Session], content: str
) -> None:
    # Arrange
    params = {
        "id": uuid.uuid4(),
        "merchant_id": uuid.uuid4(),
        "content": content,
        "session_id": uuid.uuid4(),
        "turn_id": uuid.uuid4(),
        "at": TEST_NOW,
    }

    # Act / Assert
    with pytest.raises(IntegrityError), session_factory.begin() as db:
        db.execute(INSERT_ROW, params)
    assert _row_count(session_factory) == 0
