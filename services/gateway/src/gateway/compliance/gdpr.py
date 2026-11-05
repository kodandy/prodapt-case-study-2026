"""GDPR data-subject requests (R2-10): access (export), rectification and erasure, each with a deadline.

Erasure pseudonymises the user row and crypto-shreds the TOTP secret. Ledger, transfers, balances, KYC status
and audit rows stay: they carry pseudonymous ids only and fall under AML record retention."""

import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from contracts.compliance import COMPLIANCE_TOPIC, SubjectErasedV1
from sqlalchemy import and_, delete, not_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import LoginThrottle, User, UserTotp
from gateway.accounts.service import hash_password
from gateway.audit.service import record_audit
from gateway.clock import Clock
from gateway.compliance.kyc import UnknownUserError, flag_for_review
from gateway.compliance.models import DataSubjectRequest, KycReview
from gateway.outbox.models import OutboxEvent
from gateway.wallets.models import Transfer
from gateway.wallets.service import list_wallets

DSR_DEADLINE = timedelta(days=30)
RECTIFIED_ACTION = "gdpr.rectified"
EXPORTED_ACTION = "gdpr.exported"
ERASED_ACTION = "gdpr.erased"
ERASED_EMAIL_DOMAIN = "erased.invalid"

DsrKind = Literal["access", "erasure", "rectification"]


@dataclass(frozen=True)
class ProfileView:
    user_id: uuid.UUID
    email: str
    full_name: str | None
    country: str | None
    kyc_status: str
    kyc_expires_at: datetime | None


@dataclass(frozen=True)
class DataRequestView:
    request_id: uuid.UUID
    user_id: uuid.UUID
    kind: str
    status: str
    received_at: datetime
    due_at: datetime
    completed_at: datetime | None


def read_profile(session: Session, user_id: uuid.UUID) -> ProfileView:
    user = session.get(User, user_id)
    if user is None:
        raise UnknownUserError
    return _profile(user)


def rectify_profile(
    session_factory: sessionmaker[Session],
    clock: Clock,
    user_id: uuid.UUID,
    full_name: str | None,
    country: str | None,
) -> ProfileView:
    """`None` leaves a field unchanged. Changing an existing non-null name fires the `name_change` KYC rule.
    The request row, the audit row (field names only) and any review commit together."""
    with session_factory.begin() as session:
        user = _locked_user(session, user_id)
        fields: list[str] = []
        name_changed = False
        if full_name is not None:
            fields.append("full_name")
            name_changed = user.full_name is not None and user.full_name != full_name
            user.full_name = full_name
        if country is not None:
            fields.append("country")
            user.country = country
        if not fields:
            return _profile(user)
        now = clock()
        _add_request(session, user_id, "rectification", now, {"fields": fields})
        if name_changed:
            flag_for_review(session, clock, user, ("name_change",), "rectification")
        actor = f"user:{user_id}"
        record_audit(session, clock, actor, "consumer", RECTIFIED_ACTION, actor, {"fields": fields})
        return _profile(user)


def export_subject_data(
    session_factory: sessionmaker[Session], clock: Clock, user_id: uuid.UUID
) -> dict[str, object]:
    """JSON-ready `{request_id, generated_at, data}`. Transfers name no counterparty."""
    with session_factory.begin() as session:
        user = _locked_user(session, user_id)
        now = clock()
        request = _add_request(session, user_id, "access", now, {})
        data: dict[str, object] = {
            "profile": profile_json(_profile(user)),
            "wallets": [
                {
                    "currency": wallet.currency,
                    "available_minor": wallet.available_minor,
                    "held_minor": wallet.held_minor,
                }
                for wallet in list_wallets(session, user_id)
            ],
            "transfers": _transfers_json(session, user_id),
            "kyc_reviews": [
                {
                    "review_id": str(review.id),
                    "status": review.status,
                    "reasons": list(review.reasons),
                    "opened_at": review.opened_at.isoformat(),
                    "resolved_at": _iso(review.resolved_at),
                }
                for review in session.scalars(
                    select(KycReview).where(KycReview.user_id == user_id).order_by(KycReview.opened_at)
                )
            ],
            "data_requests": [request_json(view) for view in _requests(session, user_id)],
        }
        actor = f"user:{user_id}"
        record_audit(
            session, clock, actor, "consumer", EXPORTED_ACTION, actor, {"request_id": str(request.id)}
        )
        return {"request_id": str(request.id), "generated_at": now.isoformat(), "data": data}


def erase_subject(session_factory: sessionmaker[Session], clock: Clock, user_id: uuid.UUID) -> uuid.UUID:
    """Pseudonymises the user (random placeholder email, no name/country, unusable password), deletes the TOTP
    secret and throttle rows and stamps `erased_at`, which invalidates every session.
    Returns the request id."""
    with session_factory.begin() as session:
        user = _locked_user(session, user_id)
        now = clock()
        request = _add_request(session, user_id, "erasure", now, {})
        user.email = f"erased-{secrets.token_hex(16)}@{ERASED_EMAIL_DOMAIN}"
        user.full_name = None
        user.country = None
        user.password_hash = hash_password(secrets.token_urlsafe(32))
        user.erased_at = now
        session.execute(delete(UserTotp).where(UserTotp.user_id == user_id))
        session.execute(delete(LoginThrottle).where(LoginThrottle.user_id == user_id))
        event = SubjectErasedV1(
            event_id=uuid.uuid4(),
            event_type="compliance.subject_erased",
            occurred_at=now,
            request_id=request.id,
            user_id=user_id,
        )
        session.add(
            OutboxEvent(
                event_id=event.event_id,
                topic=COMPLIANCE_TOPIC,
                payload=event.model_dump(mode="json"),
                headers={},
                created_at=now,
                published_at=None,
            )
        )
        actor = f"user:{user_id}"
        record_audit(session, clock, actor, "consumer", ERASED_ACTION, actor, {"request_id": str(request.id)})
        return request.id


def list_own_requests(session: Session, user_id: uuid.UUID) -> list[DataRequestView]:
    return _requests(session, user_id)


def list_requests_for_ops(
    session: Session, now: datetime, overdue: bool | None, limit: int
) -> list[DataRequestView]:
    """Earliest deadline first. Overdue = still `received` with the deadline already passed; `overdue=None`
    lists everything, `False` everything else."""
    query = select(DataSubjectRequest).order_by(DataSubjectRequest.due_at, DataSubjectRequest.id).limit(limit)
    is_overdue = and_(DataSubjectRequest.status == "received", DataSubjectRequest.due_at < now)
    if overdue is True:
        query = query.where(is_overdue)
    elif overdue is False:
        query = query.where(not_(is_overdue))
    return [_view(request) for request in session.scalars(query)]


def _locked_user(session: Session, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id, with_for_update=True)
    if user is None or user.erased_at is not None:
        raise UnknownUserError
    return user


def _add_request(
    session: Session, user_id: uuid.UUID, kind: DsrKind, now: datetime, detail: dict[str, object]
) -> DataSubjectRequest:
    request = DataSubjectRequest(
        id=uuid.uuid4(),
        user_id=user_id,
        kind=kind,
        status="completed",
        received_at=now,
        due_at=now + DSR_DEADLINE,
        completed_at=now,
        detail=detail,
    )
    session.add(request)
    session.flush()
    return request


def _requests(session: Session, user_id: uuid.UUID) -> list[DataRequestView]:
    query = (
        select(DataSubjectRequest)
        .where(DataSubjectRequest.user_id == user_id)
        .order_by(DataSubjectRequest.received_at, DataSubjectRequest.id)
    )
    return [_view(request) for request in session.scalars(query)]


def _view(request: DataSubjectRequest) -> DataRequestView:
    return DataRequestView(
        request_id=request.id,
        user_id=request.user_id,
        kind=request.kind,
        status=request.status,
        received_at=request.received_at,
        due_at=request.due_at,
        completed_at=request.completed_at,
    )


def _transfers_json(session: Session, user_id: uuid.UUID) -> list[dict[str, object]]:
    rows = session.scalars(
        select(Transfer)
        .where(or_(Transfer.sender_user_id == user_id, Transfer.recipient_user_id == user_id))
        .order_by(Transfer.created_at, Transfer.id)
    )
    return [
        {
            "direction": "sent" if row.sender_user_id == user_id else "received",
            "amount_minor": row.amount_minor,
            "currency": row.currency,
            "created_at": row.created_at.isoformat(),
        }
        for row in rows
    ]


def _profile(user: User) -> ProfileView:
    return ProfileView(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        country=user.country,
        kyc_status=user.kyc_status,
        kyc_expires_at=user.kyc_expires_at,
    )


def profile_json(view: ProfileView) -> dict[str, object]:
    return {
        "user_id": str(view.user_id),
        "email": view.email,
        "full_name": view.full_name,
        "country": view.country,
        "kyc_status": view.kyc_status,
        "kyc_expires_at": _iso(view.kyc_expires_at),
    }


def ops_request_json(view: DataRequestView) -> dict[str, object]:
    return {**request_json(view), "user_id": str(view.user_id)}


def request_json(view: DataRequestView) -> dict[str, object]:
    return {
        "request_id": str(view.request_id),
        "kind": view.kind,
        "status": view.status,
        "received_at": view.received_at.isoformat(),
        "due_at": view.due_at.isoformat(),
        "completed_at": _iso(view.completed_at),
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()
