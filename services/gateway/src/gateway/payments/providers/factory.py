"""Chooses the acquirer adapter from configuration (PAYMENT_PROVIDER=simulator|stripe)."""

from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock
from gateway.payments.providers.port import ProviderPort
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.providers.stripe.provider import StripeProvider

PROVIDER_NAMES = ("simulator", "stripe")


class ProviderConfigError(ValueError):
    """Unknown provider name or a missing key. Never carries the key."""


def build_provider(
    name: str, stripe_secret_key: str | None, session_factory: sessionmaker[Session], clock: Clock
) -> ProviderPort:
    if name == "simulator":
        return SimulatorProvider(session_factory, clock)
    if name == "stripe":
        if not stripe_secret_key:
            raise ProviderConfigError("PAYMENT_PROVIDER=stripe needs STRIPE_SECRET_KEY")
        return StripeProvider(stripe_secret_key)
    raise ProviderConfigError(f"PAYMENT_PROVIDER must be one of {', '.join(PROVIDER_NAMES)}")
