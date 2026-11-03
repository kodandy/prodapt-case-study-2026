import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pyotp
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import User, UserTotp
from gateway.accounts.service import UnknownUserError
from gateway.audit.models import AuditLog
from gateway.clock import Clock

TOTP_ISSUER = "PayPlat"
TOTP_STEP_SECONDS = 30
TOTP_ACCEPTED_STEP_OFFSETS = (-1, 0, 1)
TOTP_MAX_FAILURES = 5
TOTP_LOCKOUT = timedelta(minutes=15)
_CODE_PATTERN = re.compile(r"[0-9]{6}")
_NONCE_BYTES = 12


@dataclass(frozen=True)
class TotpEnrolment:
    secret: str = field(repr=False)
    otpauth_uri: str = field(repr=False)


class TotpDecryptError(Exception):
    pass


class TotpAlreadyEnabledError(Exception):
    pass


class TotpNotEnrolledError(Exception):
    pass


class InvalidTotpCodeError(Exception):
    pass


class TotpLockedError(Exception):
    pass


def encrypt_totp_secret(key: str, user_id: uuid.UUID, secret: str) -> bytes:
    """AES-256-GCM, nonce prepended; the user id is the AAD so a blob only decrypts for its own user."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_aes_key(key)).encrypt(nonce, secret.encode(), user_id.bytes)


def decrypt_totp_secret(key: str, user_id: uuid.UUID, blob: bytes) -> str:
    nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
    try:
        return AESGCM(_aes_key(key)).decrypt(nonce, ciphertext, user_id.bytes).decode()
    except (InvalidTag, ValueError) as exc:
        raise TotpDecryptError from exc


def enrol_totp(
    session_factory: sessionmaker[Session], clock: Clock, key: str, user_id: uuid.UUID
) -> TotpEnrolment:
    """Starts (or restarts) a pending enrolment. Raises TotpAlreadyEnabledError once confirmed."""
    with session_factory.begin() as session:
        email = session.scalar(select(User.email).where(User.id == user_id).with_for_update())
        if email is None:
            raise UnknownUserError
        now = clock()
        secret = pyotp.random_base32()
        blob = encrypt_totp_secret(key, user_id, secret)
        record = _lock(session, user_id)
        if record is None:
            session.add(
                UserTotp(
                    user_id=user_id,
                    secret_ciphertext=blob,
                    confirmed_at=None,
                    last_used_step=None,
                    failed_attempts=0,
                    locked_until=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        elif record.confirmed_at is not None:
            raise TotpAlreadyEnabledError
        else:
            record.secret_ciphertext = blob
            record.last_used_step = None
            record.failed_attempts = 0
            record.locked_until = None
            record.updated_at = now
        _audit(session, now, user_id, "mfa.totp_enrolled", {})
        uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=TOTP_ISSUER)
        return TotpEnrolment(secret=secret, otpauth_uri=uri)


def confirm_totp(
    session_factory: sessionmaker[Session], clock: Clock, key: str, user_id: uuid.UUID, code: str
) -> None:
    """Pending enrolment + valid code → TOTP enabled."""
    _verify(session_factory, clock, key, user_id, code, confirming=True)


def check_totp(
    session_factory: sessionmaker[Session], clock: Clock, key: str, user_id: uuid.UUID, code: str
) -> None:
    """Login-time second factor on a confirmed enrolment."""
    _verify(session_factory, clock, key, user_id, code, confirming=False)


def totp_enabled(session: Session, user_id: uuid.UUID) -> bool:
    confirmed_at = session.scalar(select(UserTotp.confirmed_at).where(UserTotp.user_id == user_id))
    return confirmed_at is not None


def _verify(
    session_factory: sessionmaker[Session],
    clock: Clock,
    key: str,
    user_id: uuid.UUID,
    code: str,
    *,
    confirming: bool,
) -> None:
    """Commits the failure counter / lock before raising, so lockout survives the error."""
    failure: Exception | None = None
    with session_factory.begin() as session:
        record = _lock(session, user_id)
        if record is None:
            raise TotpNotEnrolledError
        if confirming and record.confirmed_at is not None:
            raise TotpAlreadyEnabledError
        if not confirming and record.confirmed_at is None:
            raise TotpNotEnrolledError
        now = clock()
        if record.locked_until is not None and record.locked_until > now:
            raise TotpLockedError
        step = _matching_step(decrypt_totp_secret(key, user_id, record.secret_ciphertext), code, now)
        if step is None or (record.last_used_step is not None and step <= record.last_used_step):
            failure = _record_failure(session, record, now)
        else:
            record.last_used_step = step
            record.failed_attempts = 0
            record.locked_until = None
            if confirming:
                record.confirmed_at = now
                _audit(session, now, user_id, "mfa.totp_enabled", {})
        record.updated_at = now
    if failure is not None:
        raise failure


def _record_failure(session: Session, record: UserTotp, now: datetime) -> Exception:
    record.failed_attempts += 1
    if record.failed_attempts < TOTP_MAX_FAILURES:
        return InvalidTotpCodeError()
    record.locked_until = now + TOTP_LOCKOUT
    _audit(
        session,
        now,
        record.user_id,
        "mfa.totp_locked",
        {"locked_minutes": int(TOTP_LOCKOUT.total_seconds() // 60)},
    )
    return TotpLockedError()


def _matching_step(secret: str, code: str, now: datetime) -> int | None:
    if _CODE_PATTERN.fullmatch(code) is None:
        return None
    totp = pyotp.TOTP(secret)
    current = int(now.timestamp()) // TOTP_STEP_SECONDS
    for offset in TOTP_ACCEPTED_STEP_OFFSETS:
        step = current + offset
        if hmac.compare_digest(totp.at(step * TOTP_STEP_SECONDS), code):
            return step
    return None


def _lock(session: Session, user_id: uuid.UUID) -> UserTotp | None:
    return session.scalars(
        select(UserTotp).where(UserTotp.user_id == user_id).with_for_update()
    ).one_or_none()


def _aes_key(key: str) -> bytes:
    return hashlib.sha256(key.encode()).digest()


def _audit(
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
