"""Event schemas on the ops port (gateway outbox → Redis Streams → ops-ai). Versioned by topic suffix."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PAYMENTS_TOPIC = "gw.payments.v1"

PaymentState = Literal["created", "succeeded", "declined", "failed", "unknown"]
EventOrigin = Literal["live", "backfill"]
CARD_REF_PATTERN = r"^[0-9a-f]{32}$"


class PaymentEventV1(BaseModel):
    """Carries pseudonymous ids only: no PAN, no email, no name."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["payment.succeeded", "payment.declined", "payment.failed", "payment.unknown"]
    occurred_at: datetime
    origin: EventOrigin = "live"
    entity_version: int = Field(ge=1)
    payment_id: uuid.UUID
    merchant_id: uuid.UUID
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    method: Literal["card"]
    state: PaymentState
    provider: str | None
    decline_code: str | None
    card_bin: str | None
    card_last4: str | None
    card_ref: str | None = Field(default=None, pattern=CARD_REF_PATTERN)
    """Keyed derivation of the card fingerprint: counts distinct cards, cannot be looked up elsewhere."""
