import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated

import jwt
import pyotp
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.mfa import (
    TotpDecryptError,
    _matching_step,
    decrypt_totp_secret,
    encrypt_totp_secret,
)
from gateway.accounts.models import Merchant, OpsUser, User
from gateway.accounts.rbac import OPS_ROLES, is_allowed
from gateway.accounts.rbac import OpsRole as OpsRole
from gateway.accounts.service import (
    _DUMMY_PASSWORD_HASH,
    MerchantCredentials,
    create_merchant,
    hash_password,
    issue_api_key,
    set_password,
    sign_up,
    verify_password,
)
from gateway.accounts.sessions import _public_key_pem
from gateway.accounts.tokens import TOKEN_ALGORITHM, TOKEN_ISSUER
from gateway.audit.service import record_audit
from gateway.clock import Clock

OPS_SESSION_AUDIENCE = "gateway-ops"
OPS_SESSION_TTL_SECONDS = 900
OPS_MAX_FAILURES = 5
OPS_LOCKOUT = timedelta(minutes=15)
OPS_AMR = ("pwd", "otp")
_OPS_TOTP_KEY_CONTEXT = ":ops-totp"
_REQUIRED_CLAIMS = ["iss", "aud", "sub", "exp", "iat", "jti"]


@dataclass(frozen=True)
class OpsPrincipal:
    ops_user_id: uuid.UUID
    role: OpsRole


@dataclass(frozen=True)
class OpsEnrolment:
    ops_user_id: uuid.UUID
    totp_secret: str = field(repr=False)


class InvalidOpsCredentialsError(Exception):
    pass


class OpsLockedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("ops account locked")
        self.retry_after = retry_after


class InvalidOpsSessionError(Exception):
    pass


def bootstrap_merchant(
    session_factory: sessionmaker[Session],
    clock: Clock,
    name: str,
    api_key_pepper: str,
    merchant_id: uuid.UUID | None = None,
) -> MerchantCredentials:
    """Operator-side merchant creation (no HTTP route): commits and returns the one-time plaintext API key.

    When a merchant with `merchant_id` already exists, only a new API key is issued for it; `name` is ignored.
    """
    with session_factory.begin() as session:
        if merchant_id is not None and session.get(Merchant, merchant_id) is not None:
            return issue_api_key(session, clock, merchant_id, api_key_pepper)
        return create_merchant(session, clock, name, api_key_pepper, merchant_id)


def bootstrap_user(
    session_factory: sessionmaker[Session], clock: Clock, email: str, password: str, user_id: uuid.UUID
) -> uuid.UUID:
    """Operator-side user creation (no HTTP route): commits and returns `user_id`.

    When a user with `user_id` already exists, only its password is replaced; `email` is ignored.
    """
    with session_factory.begin() as session:
        if session.get(User, user_id) is not None:
            set_password(session, user_id, password)
            return user_id
        return sign_up(session, clock, email, password, user_id=user_id).user_id


def bootstrap_ops_user(
    session_factory: sessionmaker[Session],
    clock: Clock,
    email: str,
    password: str,
    role: OpsRole,
    totp_encryption_key: str,
    ops_user_id: uuid.UUID | None = None,
) -> OpsEnrolment:
    """Operator-side ops staff creation (no HTTP route). TOTP is enrolled at creation: the returned secret is
    shown once to the staff member, stored only as AES-GCM ciphertext. Commits."""
    if role not in OPS_ROLES:
        raise ValueError("unknown ops role")
    new_id = ops_user_id or uuid.uuid4()
    secret = pyotp.random_base32()
    now = clock()
    with session_factory.begin() as session:
        session.add(
            OpsUser(
                id=new_id,
                email=email.strip().lower(),
                password_hash=hash_password(password),
                role=role,
                totp_secret_ciphertext=encrypt_totp_secret(_ops_key(totp_encryption_key), new_id, secret),
                totp_last_used_step=None,
                failed_attempts=0,
                locked_until=None,
                disabled_at=None,
                created_at=now,
            )
        )
    return OpsEnrolment(ops_user_id=new_id, totp_secret=secret)


def authenticate_ops(
    session_factory: sessionmaker[Session],
    clock: Clock,
    totp_encryption_key: str,
    email: str,
    password: str,
    totp_code: str,
) -> OpsPrincipal:
    """Password and TOTP are both required. Unknown email, wrong password and a wrong or replayed code all
    raise InvalidOpsCredentialsError; the 5th consecutive failure locks the account for OPS_LOCKOUT
    (OpsLockedError). Failure counters and audit rows are committed before raising."""
    failure: Exception | None = None
    principal: OpsPrincipal | None = None
    with session_factory.begin() as session:
        user = session.scalars(
            select(OpsUser).where(OpsUser.email == email.strip().lower()).with_for_update()
        ).one_or_none()
        if user is None:
            verify_password(_DUMMY_PASSWORD_HASH, password)
            raise InvalidOpsCredentialsError
        now = clock()
        if user.locked_until is not None:
            if user.locked_until > now:
                raise OpsLockedError(_retry_after(user.locked_until, now))
            user.failed_attempts = 0
            user.locked_until = None
        step = _check_factors(user, totp_encryption_key, password, totp_code, now)
        actor = f"ops:{user.id}"
        if step is None or user.disabled_at is not None:
            user.failed_attempts += 1
            if user.failed_attempts >= OPS_MAX_FAILURES:
                user.locked_until = now + OPS_LOCKOUT
                record_audit(
                    session,
                    clock,
                    actor,
                    user.role,
                    "ops.login_locked",
                    actor,
                    {"locked_minutes": int(OPS_LOCKOUT.total_seconds() // 60)},
                )
                failure = OpsLockedError(_retry_after(user.locked_until, now))
            else:
                record_audit(session, clock, actor, user.role, "ops.login_failed", actor, {})
                failure = InvalidOpsCredentialsError()
        else:
            user.totp_last_used_step = step
            user.failed_attempts = 0
            user.locked_until = None
            record_audit(session, clock, actor, user.role, "ops.login", actor, {"amr": list(OPS_AMR)})
            principal = OpsPrincipal(ops_user_id=user.id, role=_as_ops_role(user.role))
    if failure is not None or principal is None:
        raise failure or InvalidOpsCredentialsError()
    return principal


def issue_ops_session_token(private_key_pem: str, principal: OpsPrincipal, now: datetime) -> str:
    issued_at = int(now.timestamp())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": OPS_SESSION_AUDIENCE,
        "sub": f"ops:{principal.ops_user_id}",
        "ops_user_id": str(principal.ops_user_id),
        "role": principal.role,
        "amr": list(OPS_AMR),
        "iat": issued_at,
        "exp": issued_at + OPS_SESSION_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm=TOKEN_ALGORITHM)


def verify_ops_session_token(private_key_pem: str, token: str, now: datetime) -> OpsPrincipal:
    """Raises InvalidOpsSessionError for anything but a live ops session (a consumer session or ops-ai
    token has another audience). Expiry is checked against the injected `now`."""
    try:
        claims = jwt.decode(
            token,
            _public_key_pem(private_key_pem),
            algorithms=[TOKEN_ALGORITHM],
            audience=OPS_SESSION_AUDIENCE,
            issuer=TOKEN_ISSUER,
            options={"require": _REQUIRED_CLAIMS, "verify_exp": False, "verify_iat": False},
        )
    except jwt.PyJWTError as exc:
        raise InvalidOpsSessionError from exc
    expires_at = claims["exp"]
    if not isinstance(expires_at, int) or expires_at <= int(now.timestamp()):
        raise InvalidOpsSessionError
    raw_id = claims.get("ops_user_id")
    role = claims.get("role")
    if not isinstance(raw_id, str) or role not in OPS_ROLES or not isinstance(role, str):
        raise InvalidOpsSessionError
    if claims["sub"] != f"ops:{raw_id}" or claims.get("amr") != list(OPS_AMR):
        raise InvalidOpsSessionError
    try:
        ops_user_id = uuid.UUID(raw_id)
    except ValueError as exc:
        raise InvalidOpsSessionError from exc
    if str(ops_user_id) != raw_id:
        raise InvalidOpsSessionError
    return OpsPrincipal(ops_user_id=ops_user_id, role=_as_ops_role(role))


class OpsGuard:
    """FastAPI dependency factory for ops routes. The token's role must equal the stored role and the
    user must
    not be disabled, so demotion or disabling takes effect on the next request, not at token expiry."""

    def __init__(
        self, session_factory: sessionmaker[Session], clock: Clock, private_key_pem: str | None
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock
        self._private_key_pem = private_key_pem
        self._bearer = HTTPBearer(auto_error=False)

    def require(self, action: str) -> Callable[..., OpsPrincipal]:
        bearer = self._bearer

        def dependency(
            request: Request,
            credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        ) -> OpsPrincipal:
            principal = self.authenticate(None if credentials is None else credentials.credentials)
            if not is_allowed(principal.role, action):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
            request.state.ops_principal = principal
            return principal

        return dependency

    def authenticate(self, token: str | None) -> OpsPrincipal:
        invalid = HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid ops session")
        if token is None or self._private_key_pem is None:
            raise invalid
        try:
            principal = verify_ops_session_token(self._private_key_pem, token, self._clock())
        except InvalidOpsSessionError:
            raise invalid from None
        with self._session_factory() as session:
            row = session.execute(
                select(OpsUser.role, OpsUser.disabled_at).where(OpsUser.id == principal.ops_user_id)
            ).one_or_none()
        if row is None or row.disabled_at is not None or row.role != principal.role:
            raise invalid
        return principal


def _check_factors(
    user: OpsUser, totp_encryption_key: str, password: str, totp_code: str, now: datetime
) -> int | None:
    """The matched TOTP step when both factors pass, else None. Argon2 always runs, so timing does not tell
    a wrong password from a wrong code."""
    password_ok = verify_password(user.password_hash, password)
    try:
        secret = decrypt_totp_secret(_ops_key(totp_encryption_key), user.id, user.totp_secret_ciphertext)
    except TotpDecryptError:
        return None
    step = _matching_step(secret, totp_code, now)
    if not password_ok or step is None:
        return None
    if user.totp_last_used_step is not None and step <= user.totp_last_used_step:
        return None
    return step


def _ops_key(totp_encryption_key: str) -> str:
    return totp_encryption_key + _OPS_TOTP_KEY_CONTEXT


def _retry_after(locked_until: datetime, now: datetime) -> int:
    return max(1, int((locked_until - now).total_seconds()))


def _as_ops_role(role: str) -> OpsRole:
    for candidate in OPS_ROLES:
        if candidate == role:
            return candidate
    raise InvalidOpsSessionError
