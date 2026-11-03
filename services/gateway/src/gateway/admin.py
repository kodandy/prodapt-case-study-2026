from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.service import MerchantCredentials, create_merchant
from gateway.clock import Clock


def bootstrap_merchant(
    session_factory: sessionmaker[Session], clock: Clock, name: str, api_key_pepper: str
) -> MerchantCredentials:
    """Operator-side merchant creation (no HTTP route): commits and returns the one-time plaintext API key."""
    with session_factory.begin() as session:
        return create_merchant(session, clock, name, api_key_pepper)
