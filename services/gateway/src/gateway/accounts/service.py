import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gateway.accounts.models import ApiKey, Merchant, User
from gateway.accounts.rbac import MERCHANT_ROLES, MerchantRole
from gateway.clock import Clock
from gateway.payments.ledger.service import ensure_account
from gateway.wallets.models import AccountBalance

WALLET_KIND = "wallet"
API_KEY_PREFIX = "sk_test_"

_password_hasher = PasswordHasher()


@dataclass(frozen=True)
class SignUpResult:
    user_id: uuid.UUID
    wallet_account_id: uuid.UUID


@dataclass(frozen=True)
class MerchantCredentials:
    merchant_id: uuid.UUID
    api_key: str
    """Plaintext API key: returned once, stored only as a keyed hash."""


@dataclass(frozen=True)
class LoginRecord:
    user_id: uuid.UUID
    password_hash: str = field(repr=False)


class EmailAlreadyRegisteredError(Exception):
    pass


class UnknownMerchantError(Exception):
    pass


class UnknownUserError(Exception):
    pass


def hash_password(password: str) -> str:
    """argon2id hash."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))
"""Verified against for an unknown email, so both login failures cost one argon2 verification."""


def sign_up(
    session: Session,
    clock: Clock,
    email: str,
    password: str,
    currency: str = "USD",
    user_id: uuid.UUID | None = None,
) -> SignUpResult:
    """Creates the user (under `user_id`, else a random id) and one wallet ledger account (with a zero
    balance row) in `currency`.

    Raises EmailAlreadyRegisteredError for a duplicate email (case-insensitive). Does not commit.
    """
    normalized = email.strip().lower()
    existing = session.scalar(select(User.id).where(func.lower(User.email) == normalized))
    if existing is not None:
        raise EmailAlreadyRegisteredError
    user = User(
        id=user_id or uuid.uuid4(),
        email=normalized,
        password_hash=hash_password(password),
        created_at=clock(),
    )
    session.add(user)
    try:
        with session.begin_nested():
            session.flush()
    except IntegrityError as exc:
        raise EmailAlreadyRegisteredError from exc
    wallet_id = ensure_account(session, "user", user.id, WALLET_KIND, currency)
    session.add(AccountBalance(account_id=wallet_id, available_minor=0, held_minor=0))
    session.flush()
    return SignUpResult(user_id=user.id, wallet_account_id=wallet_id)


def find_login(session: Session, email: str) -> LoginRecord | None:
    """The login record for `email`, normalized as in `sign_up`. Stored emails are normalized, so the column
    is compared directly and the unique index serves the lookup."""
    row = session.execute(
        select(User.id, User.password_hash).where(User.email == email.strip().lower())
    ).one_or_none()
    if row is None:
        return None
    return LoginRecord(user_id=row.id, password_hash=row.password_hash)


def verify_login(record: LoginRecord | None, password: str) -> uuid.UUID | None:
    """The user id when `password` matches the record, else None. Needs no session. For no record it still
    runs one argon2 verification against a dummy hash, so both login failures cost the same."""
    if record is None:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    return record.user_id if verify_password(record.password_hash, password) else None


def authenticate_user(session: Session, email: str, password: str) -> uuid.UUID | None:
    """The user id for a matching email (case-insensitive) and correct password, else None."""
    return verify_login(find_login(session, email), password)


def set_password(session: Session, user_id: uuid.UUID, password: str) -> None:
    """Operator-side password replacement. Raises UnknownUserError for an unknown id. Does not commit."""
    user = session.get(User, user_id)
    if user is None:
        raise UnknownUserError
    user.password_hash = hash_password(password)
    session.flush()


def _hash_api_key(api_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), api_key.encode(), hashlib.sha256).hexdigest()


def create_merchant(
    session: Session,
    clock: Clock,
    name: str,
    api_key_pepper: str,
    merchant_id: uuid.UUID | None = None,
    role: MerchantRole = "merchant_admin",
) -> MerchantCredentials:
    """Creates the merchant (under `merchant_id`, else a random id) and an API key `sk_test_...`.

    Does not commit.
    """
    merchant = Merchant(id=merchant_id or uuid.uuid4(), name=name, created_at=clock())
    session.add(merchant)
    session.flush()
    return MerchantCredentials(
        merchant_id=merchant.id, api_key=_add_api_key(session, clock, merchant.id, api_key_pepper, role)
    )


def issue_api_key(
    session: Session,
    clock: Clock,
    merchant_id: uuid.UUID,
    api_key_pepper: str,
    role: MerchantRole = "merchant_admin",
) -> MerchantCredentials:
    """A new API key for an existing merchant; earlier keys keep working.

    Raises UnknownMerchantError when the merchant does not exist. Does not commit.
    """
    if session.get(Merchant, merchant_id) is None:
        raise UnknownMerchantError
    return MerchantCredentials(
        merchant_id=merchant_id, api_key=_add_api_key(session, clock, merchant_id, api_key_pepper, role)
    )


def _add_api_key(
    session: Session, clock: Clock, merchant_id: uuid.UUID, api_key_pepper: str, role: MerchantRole
) -> str:
    if role not in MERCHANT_ROLES:
        raise ValueError("unknown merchant role")
    api_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    session.add(
        ApiKey(
            id=uuid.uuid4(),
            merchant_id=merchant_id,
            key_hash=_hash_api_key(api_key, api_key_pepper),
            prefix=api_key[: len(API_KEY_PREFIX) + 4],
            created_at=clock(),
            role=role,
        )
    )
    session.flush()
    return api_key


def authenticate_api_key(session: Session, api_key: str, api_key_pepper: str) -> uuid.UUID | None:
    """Returns the merchant id for a valid key, else None."""
    if not api_key.startswith(API_KEY_PREFIX):
        return None
    return session.scalar(
        select(ApiKey.merchant_id).where(ApiKey.key_hash == _hash_api_key(api_key, api_key_pepper))
    )


def authenticate_api_key_role(
    session: Session, api_key: str, api_key_pepper: str
) -> tuple[uuid.UUID, MerchantRole] | None:
    """The merchant id and the key's role for a valid key, else None. An unknown stored role is refused."""
    if not api_key.startswith(API_KEY_PREFIX):
        return None
    row = session.execute(
        select(ApiKey.merchant_id, ApiKey.role).where(
            ApiKey.key_hash == _hash_api_key(api_key, api_key_pepper)
        )
    ).one_or_none()
    if row is None:
        return None
    for role in MERCHANT_ROLES:
        if role == row.role:
            return row.merchant_id, role
    return None
