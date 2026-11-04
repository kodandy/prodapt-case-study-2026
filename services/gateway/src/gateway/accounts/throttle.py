import math
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from gateway.accounts.models import LoginThrottle
from gateway.audit.models import AuditLog

LOGIN_MAX_FAILURES = 10
LOGIN_FAILURE_WINDOW = timedelta(minutes=15)
LOGIN_LOCKOUT = timedelta(minutes=15)


class AccountLockedError(Exception):
    """The row was locked when a request that passed the early check came to write it."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(retry_after)
        self.retry_after = retry_after


def lock_retry_after(session: Session, user_id: uuid.UUID, now: datetime) -> int | None:
    """The whole seconds left of an active lock (at least 1), else None."""
    row = session.get(LoginThrottle, user_id)
    return None if row is None else _retry_after(row, now)


def record_password_failure(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    """Counts one wrong password and writes its `auth.login_failed` row in the caller's transaction; the
    LOGIN_MAX_FAILURES-th failure in a window locks the account and writes `auth.login_locked`. Raises
    AccountLockedError, counting and auditing nothing, when another request locked the account meanwhile."""
    session.execute(
        insert(LoginThrottle)
        .values(user_id=user_id, failed_attempts=0, updated_at=now)
        .on_conflict_do_nothing(index_elements=[LoginThrottle.user_id])
    )
    row = _locked_row(session, user_id)
    retry_after = _retry_after(row, now)
    if retry_after is not None:
        raise AccountLockedError(retry_after)
    _add_audit(session, now, user_id, "auth.login_failed", {"factor": "password"})
    row.updated_at = now
    lock_expired = row.locked_until is not None
    window_over = row.window_started_at is None or now - row.window_started_at > LOGIN_FAILURE_WINDOW
    if lock_expired or window_over:
        row.failed_attempts = 0
        row.window_started_at = now
        row.locked_until = None
    row.failed_attempts += 1
    if row.failed_attempts >= LOGIN_MAX_FAILURES:
        row.locked_until = now + LOGIN_LOCKOUT
        _add_audit(session, now, user_id, "auth.login_locked", {})


def settle_login_success(session: Session, user_id: uuid.UUID, now: datetime) -> None:
    """Resets the counter after a password success, when there is one. Raises AccountLockedError, leaving the
    lock in place, when another request locked the account while this one checked its password."""
    row = session.scalars(
        select(LoginThrottle).where(LoginThrottle.user_id == user_id).with_for_update()
    ).one_or_none()
    if row is None:
        return
    retry_after = _retry_after(row, now)
    if retry_after is not None:
        raise AccountLockedError(retry_after)
    if row.failed_attempts == 0 and row.locked_until is None:
        return
    row.failed_attempts = 0
    row.window_started_at = None
    row.locked_until = None
    row.updated_at = now


def _retry_after(row: LoginThrottle, now: datetime) -> int | None:
    if row.locked_until is None or row.locked_until <= now:
        return None
    return max(1, math.ceil((row.locked_until - now).total_seconds()))


def _locked_row(session: Session, user_id: uuid.UUID) -> LoginThrottle:
    query = select(LoginThrottle).where(LoginThrottle.user_id == user_id).with_for_update()
    return session.scalars(query).one()


def _add_audit(
    session: Session, now: datetime, user_id: uuid.UUID, action: str, summary: dict[str, object]
) -> None:
    session.add(
        AuditLog(
            actor_id=f"user:{user_id}",
            actor_role="consumer",
            action=action,
            target=f"user:{user_id}",
            summary=summary,
            correlation_id=uuid.uuid4().hex,
            occurred_at=now,
        )
    )
