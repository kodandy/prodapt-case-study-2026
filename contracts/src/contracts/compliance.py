"""Compliance events on the ops port (gateway outbox → Redis Streams → ops-ai). Pseudonymous ids only."""

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from contracts.events import EventOrigin

COMPLIANCE_TOPIC = "gw.compliance.v1"

KycRuleName = Literal[
    "amount_threshold",
    "volume_24h",
    "high_risk_country_pair",
    "kyc_expired",
    "kyc_stale",
    "velocity",
    "name_change",
]


class KycReviewRequiredV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["compliance.kyc_review_required"]
    occurred_at: datetime
    origin: EventOrigin = "live"
    review_id: uuid.UUID
    user_id: uuid.UUID
    rules: tuple[KycRuleName, ...] = Field(min_length=1)


class SubjectErasedV1(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: uuid.UUID
    event_type: Literal["compliance.subject_erased"]
    occurred_at: datetime
    origin: EventOrigin = "live"
    request_id: uuid.UUID
    user_id: uuid.UUID


ComplianceEventV1 = Annotated[KycReviewRequiredV1 | SubjectErasedV1, Field(discriminator="event_type")]

_COMPLIANCE_EVENT_ADAPTER: TypeAdapter[KycReviewRequiredV1 | SubjectErasedV1] = TypeAdapter(ComplianceEventV1)


def parse_compliance_event(payload: Mapping[str, object]) -> KycReviewRequiredV1 | SubjectErasedV1:
    """Raises pydantic.ValidationError for an unknown or missing `event_type` or any schema violation."""
    return _COMPLIANCE_EVENT_ADAPTER.validate_python(dict(payload))
