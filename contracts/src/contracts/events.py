"""Event schemas on the ops port (gateway outbox → Redis Streams → ops-ai). Versioned by topic suffix."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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


NOTIFICATIONS_TOPIC = "gw.notifications.v1"

NotificationKind = Literal["payment_confirmation", "suspicious_activity"]


class NotificationEventV1(BaseModel):
    """A notification record was created. Pseudonymous ids only: no PAN, BIN, last4 or email."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["notification.created"]
    occurred_at: datetime
    origin: EventOrigin = "live"
    notification_id: uuid.UUID
    kind: NotificationKind
    source_event_id: uuid.UUID
    recipient_type: Literal["merchant"]
    recipient_id: uuid.UUID
    payment_id: uuid.UUID
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)


DISPUTES_TOPIC = "gw.disputes.v1"

DisputeEventState = Literal["opened", "under_review", "won", "lost"]
DisputeActorRole = Literal["card_network", "consumer", "merchant", "ops_lead"]
DisputeEventType = Literal["dispute.opened", "dispute.evidence_submitted", "dispute.resolved"]
NETWORK_REASON_CODE_PATTERN = r"^[0-9]{1,2}(\.[0-9]{1,2}){1,2}$"
DISPUTE_MAX_AMOUNT_MINOR = 10**12
"""The gateway API ceiling (gateway.payments.schemas.MAX_AMOUNT_MINOR); nothing larger is a real dispute."""
_EVENT_STATES: dict[DisputeEventType, frozenset[DisputeEventState]] = {
    "dispute.opened": frozenset({"opened"}),
    "dispute.evidence_submitted": frozenset({"under_review"}),
    "dispute.resolved": frozenset({"won", "lost"}),
}
_STATE_VERSIONS: dict[DisputeEventState, int] = {"opened": 1, "under_review": 2, "won": 3, "lost": 3}


class DisputeEventV1(BaseModel):
    """One dispute workflow transition (`entity_version` = its position in the workflow, 1-based).

    Ids, amounts, states, an actor category and a network reason code only: no PAN, BIN or last4, no payer id,
    no free-text reason and no evidence text. `from_state` is what the gateway saw, so the consumer's judge
    still checks that the sequence is legal; only `event_type` must match `to_state`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    event_type: DisputeEventType
    occurred_at: AwareDatetime
    origin: EventOrigin = "live"
    entity_version: int = Field(ge=1, le=3)
    dispute_id: uuid.UUID
    payment_id: uuid.UUID
    merchant_id: uuid.UUID
    from_state: DisputeEventState | None
    to_state: DisputeEventState
    actor_role: DisputeActorRole
    network_reason_code: str | None = Field(pattern=NETWORK_REASON_CODE_PATTERN)
    """The dispute reason when it is a card-network code (e.g. "13.1"); None for a free-text reason."""
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    payment_amount_minor: int = Field(gt=0, le=DISPUTE_MAX_AMOUNT_MINOR)
    refunded_minor: int = Field(ge=0, le=DISPUTE_MAX_AMOUNT_MINOR)
    dispute_amount_minor: int = Field(gt=0, le=DISPUTE_MAX_AMOUNT_MINOR)
    chargeback_minor: int = Field(ge=0, le=DISPUTE_MAX_AMOUNT_MINOR)
    chargeback_fee_minor: int = Field(ge=0, le=DISPUTE_MAX_AMOUNT_MINOR)

    @model_validator(mode="after")
    def _event_type_matches_state(self) -> "DisputeEventV1":
        if self.to_state not in _EVENT_STATES[self.event_type]:
            raise ValueError(f"event_type {self.event_type} does not match to_state {self.to_state}")
        if self.entity_version != _STATE_VERSIONS[self.to_state]:
            raise ValueError(f"entity_version {self.entity_version} does not match to_state {self.to_state}")
        return self
