import pytest
from gateway.payments.fx.service import seed_demo_rates
from sqlalchemy.orm import Session, sessionmaker


@pytest.fixture
def session_factory(session_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    """The root fixture truncates every gateway table; put back the migration-seeded demo FX rates."""
    with session_factory.begin() as session:
        seed_demo_rates(session)
    return session_factory
