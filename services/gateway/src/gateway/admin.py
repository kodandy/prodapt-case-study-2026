import uuid

from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import Merchant, User
from gateway.accounts.service import (
    MerchantCredentials,
    create_merchant,
    issue_api_key,
    set_password,
    sign_up,
)
from gateway.clock import Clock


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
