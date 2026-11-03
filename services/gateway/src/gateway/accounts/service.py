import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from gateway.accounts.models import ApiKey, Merchant, User
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


def authenticate_user(session: Session, email: str, password: str) -> uuid.UUID | None:
    """The user id for a matching email (case-insensitive) and correct password, else None."""
    row = session.execute(
        select(User.id, User.password_hash).where(func.lower(User.email) == email.strip().lower())
    ).one_or_none()
    if row is None:
        verify_password(_DUMMY_PASSWORD_HASH, password)
        return None
    return row.id if verify_password(row.password_hash, password) else None


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
    session: Session, clock: Clock, name: str, api_key_pepper: str, merchant_id: uuid.UUID | None = None
) -> MerchantCredentials:
    """Creates the merchant (under `merchant_id`, else a random id) and an API key `sk_test_...`.

    Does not commit.
    """
    merchant = Merchant(id=merchant_id or uuid.uuid4(), name=name, created_at=clock())
    session.add(merchant)
    session.flush()
    return MerchantCredentials(
        merchant_id=merchant.id, api_key=_add_api_key(session, clock, merchant.id, api_key_pepper)
    )


def issue_api_key(
    session: Session, clock: Clock, merchant_id: uuid.UUID, api_key_pepper: str
) -> MerchantCredentials:
    """A new API key for an existing merchant; earlier keys keep working.

    Raises UnknownMerchantError when the merchant does not exist. Does not commit.
    """
    if session.get(Merchant, merchant_id) is None:
        raise UnknownMerchantError
    return MerchantCredentials(
        merchant_id=merchant_id, api_key=_add_api_key(session, clock, merchant_id, api_key_pepper)
    )


def _add_api_key(session: Session, clock: Clock, merchant_id: uuid.UUID, api_key_pepper: str) -> str:
    api_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    session.add(
        ApiKey(
            id=uuid.uuid4(),
            merchant_id=merchant_id,
            key_hash=_hash_api_key(api_key, api_key_pepper),
            prefix=api_key[: len(API_KEY_PREFIX) + 4],
            created_at=clock(),
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
