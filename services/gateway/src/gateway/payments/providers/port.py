from dataclasses import dataclass
from typing import Literal, Protocol

ProviderStatus = Literal["succeeded", "declined"]


@dataclass(frozen=True)
class CardRef:
    """What the gateway sends to an acquirer: the token plus BIN/last4, never the PAN."""

    token: str
    bin: str
    last4: str


@dataclass(frozen=True)
class ProviderResult:
    status: ProviderStatus
    provider_ref: str
    decline_code: str | None


class ProviderTimeoutError(Exception):
    """The provider did not answer in time: the outcome is unknown, never failed."""


class ProviderRequestError(Exception):
    """The provider rejected the request itself and it is not a card decline (bad request, auth). The charge
    flow records the outcome as unknown rather than declaring a failure it cannot prove."""


class ProviderPort(Protocol):
    name: str

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        """Idempotent by provider_key: a repeated key returns the first result without charging again."""
        ...

    def lookup(self, provider_key: str) -> ProviderResult | None: ...
