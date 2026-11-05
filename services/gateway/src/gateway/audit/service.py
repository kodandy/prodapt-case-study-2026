import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from gateway.audit.models import AuditLog
from gateway.clock import Clock


def record_audit(
    session: Session,
    clock: Clock,
    actor_id: str,
    actor_role: str,
    action: str,
    target: str,
    summary: dict[str, object],
) -> None:
    """Adds the audit row to the caller's transaction and flushes it, so a failing insert raises here and the
    caller's whole operation rolls back with it (P0-10). Does not commit."""
    session.add(
        AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action=action,
            target=target,
            summary=summary,
            correlation_id=uuid.uuid4().hex,
            occurred_at=clock(),
        )
    )
    session.flush()


def list_audit(session: Session, target: str | None, limit: int) -> list[dict[str, object]]:
    """Newest first, optionally for one target; JSON-ready rows."""
    query = select(AuditLog).order_by(AuditLog.occurred_at.desc(), AuditLog.id.desc()).limit(limit)
    if target is not None:
        query = query.where(AuditLog.target == target)
    return [
        {
            "id": row.id,
            "actor_id": row.actor_id,
            "actor_role": row.actor_role,
            "action": row.action,
            "target": row.target,
            "summary": row.summary,
            "correlation_id": row.correlation_id,
            "occurred_at": row.occurred_at.isoformat(),
        }
        for row in session.scalars(query)
    ]
