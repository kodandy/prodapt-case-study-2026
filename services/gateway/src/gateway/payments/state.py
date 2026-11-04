from typing import Literal

PaymentState = Literal["created", "succeeded", "declined", "failed", "unknown"]

FINALIZABLE_STATES: tuple[PaymentState, ...] = ("created", "unknown")

ATTEMPT_LEASE_SECONDS = 30
