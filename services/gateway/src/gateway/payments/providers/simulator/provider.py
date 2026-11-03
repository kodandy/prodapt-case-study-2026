import uuid
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.payments.providers.port import CardRef, ProviderResult, ProviderStatus
from gateway.payments.providers.simulator.models import SimulatedCharge

DECLINES_BY_CARD: dict[tuple[str, str], str] = {
    ("400000", "9995"): "insufficient_funds",
    ("400000", "0002"): "card_declined",
}


class SimulatorProvider:
    """In-process acquirer simulator with its own persisted charge ledger (schema `simulator`).

    Plays the network's answer by test card (BIN + last4): see DECLINES_BY_CARD.
    """

    def __init__(self, session_factory: sessionmaker[Session], clock: Clock, name: str = "acq_a") -> None:
        self.name = name
        self._session_factory = session_factory
        self._clock = clock

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        decline_code = DECLINES_BY_CARD.get((card.bin, card.last4))
        with self._session_factory.begin() as session:
            session.execute(
                insert(SimulatedCharge)
                .values(
                    provider_key=provider_key,
                    provider=self.name,
                    amount_minor=amount_minor,
                    currency=currency,
                    card_token=card.token,
                    result="declined" if decline_code else "succeeded",
                    decline_code=decline_code,
                    provider_ref=f"{self.name}_{uuid.uuid4().hex[:20]}",
                    created_at=self._clock(),
                )
                .on_conflict_do_nothing(index_elements=["provider_key"])
            )
            stored = session.get_one(SimulatedCharge, provider_key)
            return _to_result(stored)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        with self._session_factory() as session:
            stored = session.get(SimulatedCharge, provider_key)
            return None if stored is None else _to_result(stored)

    def charge_count(self) -> int:
        """Number of distinct charges the simulator has performed (all keys)."""
        with self._session_factory() as session:
            return session.scalar(select(func.count()).select_from(SimulatedCharge)) or 0


def _to_result(stored: SimulatedCharge) -> ProviderResult:
    return ProviderResult(
        status=cast(ProviderStatus, stored.result),
        provider_ref=stored.provider_ref,
        decline_code=stored.decline_code,
    )
