"""Long-term merchant memory (B10.3). Every query is filtered by the auth scope's merchant."""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ops_ai.memory.models import MerchantMemoryRecord
from ops_ai.memory.session import PendingMemory, SessionOwner
from ops_ai.scope import Scope

RECALL_LIMIT = 20
DELETED_CONTENT = "[deleted]"
_log = logging.getLogger(__name__)


class DuplicateMemoryError(Exception):
    pass


@dataclass(frozen=True)
class MemoryRecord:
    memory_id: uuid.UUID
    content: str
    source_session_id: uuid.UUID
    source_turn_id: uuid.UUID
    supersedes_id: uuid.UUID | None
    created_at: datetime


def _record(row: MerchantMemoryRecord) -> MemoryRecord:
    return MemoryRecord(
        memory_id=row.memory_id,
        content=row.content,
        source_session_id=row.source_session_id,
        source_turn_id=row.source_turn_id,
        supersedes_id=row.supersedes_id,
        created_at=row.created_at,
    )


def save_confirmed_memory(
    db: Session,
    scope: Scope,
    owner: SessionOwner,
    session_id: uuid.UUID,
    proposal: PendingMemory,
    now: datetime,
) -> MemoryRecord:
    """DuplicateMemoryError when this source turn already produced a memory (one row per proposal, ever)."""
    memory_id = uuid.uuid4()
    inserted = db.execute(
        insert(MerchantMemoryRecord)
        .values(
            memory_id=memory_id,
            merchant_id=scope.merchant_id,
            created_by=owner.user_id,
            content=proposal.content,
            source_session_id=session_id,
            source_turn_id=proposal.source_turn_id,
            supersedes_id=None,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(MerchantMemoryRecord.memory_id)
    ).scalar_one_or_none()
    db.commit()
    if inserted is None:
        _log.info("merchant_memory.confirm_duplicate", extra={"merchant_id": str(scope.merchant_id)})
        raise DuplicateMemoryError("source turn already saved")
    _log.info(
        "merchant_memory.confirmed",
        extra={"merchant_id": str(scope.merchant_id), "memory_id": str(memory_id), "actor": owner.user_id},
    )
    return MemoryRecord(
        memory_id=memory_id,
        content=proposal.content,
        source_session_id=session_id,
        source_turn_id=proposal.source_turn_id,
        supersedes_id=None,
        created_at=now,
    )


def recall_memories(db: Session, scope: Scope, limit: int = RECALL_LIMIT) -> list[MemoryRecord]:
    rows = db.scalars(
        select(MerchantMemoryRecord)
        .where(
            MerchantMemoryRecord.merchant_id == scope.merchant_id,
            MerchantMemoryRecord.superseded_at.is_(None),
            MerchantMemoryRecord.deleted_at.is_(None),
        )
        .order_by(MerchantMemoryRecord.created_at.desc(), MerchantMemoryRecord.memory_id)
        .limit(limit)
    ).all()
    return [_record(row) for row in rows]


def _active_row(db: Session, scope: Scope, memory_id: uuid.UUID) -> MerchantMemoryRecord | None:
    return db.scalars(
        select(MerchantMemoryRecord)
        .where(
            MerchantMemoryRecord.memory_id == memory_id,
            MerchantMemoryRecord.merchant_id == scope.merchant_id,
            MerchantMemoryRecord.superseded_at.is_(None),
            MerchantMemoryRecord.deleted_at.is_(None),
        )
        .with_for_update()
    ).one_or_none()


def correct_memory(
    db: Session, scope: Scope, owner: SessionOwner, memory_id: uuid.UUID, content: str, now: datetime
) -> MemoryRecord | None:
    old = _active_row(db, scope, memory_id)
    if old is None:
        db.rollback()
        return None
    old.superseded_at = now
    new = MerchantMemoryRecord(
        memory_id=uuid.uuid4(),
        merchant_id=scope.merchant_id,
        created_by=owner.user_id,
        content=content,
        source_session_id=old.source_session_id,
        source_turn_id=old.source_turn_id,
        supersedes_id=old.memory_id,
        created_at=now,
        superseded_at=None,
        deleted_at=None,
        deleted_by=None,
    )
    db.add(new)
    db.commit()
    _log.info(
        "merchant_memory.corrected",
        extra={
            "merchant_id": str(scope.merchant_id),
            "memory_id": str(new.memory_id),
            "actor": owner.user_id,
        },
    )
    return _record(new)


def delete_memory(
    db: Session, scope: Scope, owner: SessionOwner, memory_id: uuid.UUID, now: datetime
) -> bool:
    row = _active_row(db, scope, memory_id)
    if row is None:
        db.rollback()
        return False
    row.deleted_at = now
    row.deleted_by = owner.user_id
    chain = [row.memory_id]
    ancestor = row.supersedes_id
    while ancestor is not None:
        chain.append(ancestor)
        ancestor = db.scalar(
            select(MerchantMemoryRecord.supersedes_id).where(
                MerchantMemoryRecord.memory_id == ancestor,
                MerchantMemoryRecord.merchant_id == scope.merchant_id,
            )
        )
    # erasure beats keeping the text: the audit trail keeps ids, times and actors, not the content
    db.execute(
        update(MerchantMemoryRecord)
        .where(
            MerchantMemoryRecord.memory_id.in_(chain), MerchantMemoryRecord.merchant_id == scope.merchant_id
        )
        .values(content=DELETED_CONTENT)
    )
    db.commit()
    _log.info(
        "merchant_memory.deleted",
        extra={"merchant_id": str(scope.merchant_id), "memory_id": str(memory_id), "actor": owner.user_id},
    )
    return True
