from dataclasses import dataclass
from typing import Literal, Protocol

RailStatus = Literal["succeeded", "failed", "unknown"]


@dataclass(frozen=True)
class RailResult:
    status: RailStatus
    failure_code: str | None = None


class RailPort(Protocol):
    """An external payout rail (bank transfer, UPI). `payout_id` is the rail-side idempotency key."""

    name: str

    def submit(
        self, payout_id: str, method: str, amount_minor: int, currency: str, destination: str
    ) -> RailResult: ...
