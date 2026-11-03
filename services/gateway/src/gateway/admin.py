import uuid

from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.models import Merchant
from gateway.accounts.service import MerchantCredentials, create_merchant, issue_api_key
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
