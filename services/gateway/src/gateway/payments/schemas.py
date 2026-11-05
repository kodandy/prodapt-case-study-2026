import unicodedata
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt, field_validator, model_validator

from gateway.payments.state import PaymentState
from gateway.payments.tokenization.cards import TEST_PANS, luhn_valid


class CardInput(BaseModel):
    """`number` must pass Luhn and be on the test-card allowlist; otherwise validation fails."""

    model_config = ConfigDict(extra="forbid")

    number: SecretStr
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=2000, le=2100)
    cvc: SecretStr

    @field_validator("number")
    @classmethod
    def _test_card_only(cls, value: SecretStr) -> SecretStr:
        pan = value.get_secret_value()
        if not luhn_valid(pan):
            raise ValueError("card number fails the Luhn check")
        if pan not in TEST_PANS:
            raise ValueError("only test card numbers are accepted")
        return value


MAX_AMOUNT_MINOR = 10**12


class ChargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR)
    currency: Literal["USD", "EUR", "JPY", "INR"]
    method: Literal["card"] = "card"
    card: CardInput
    settlement_currency: Literal["USD", "EUR", "JPY", "INR"] | None = None
    """Currency the merchant is credited in; absent or equal to `currency` → no FX."""


class CardSummary(BaseModel):
    bin: str
    last4: str


class ChargeResponse(BaseModel):
    payment_id: uuid.UUID
    state: PaymentState
    amount_minor: int
    currency: str
    method: Literal["card"]
    card: CardSummary
    decline_code: str | None


class PaymentSummary(BaseModel):
    """A history row. `card` carries BIN + last4 only."""

    payment_id: uuid.UUID
    state: PaymentState
    amount_minor: int
    currency: str
    method: str
    card: CardSummary | None
    decline_code: str | None
    refunded_minor: int
    disputed_minor: int
    chargeback_minor: int
    created_at: datetime


class PaymentRiskView(BaseModel):
    """`blocked`: declined by the risk rules before reaching the provider."""

    payment_id: uuid.UUID
    risk_flagged: bool
    blocked: bool


class RefundRequest(BaseModel):
    """`amount_minor` omitted → refund the full refundable amount; an explicit null is rejected."""

    model_config = ConfigDict(extra="forbid")

    amount_minor: StrictInt | None = Field(default=None, gt=0, le=MAX_AMOUNT_MINOR)

    @model_validator(mode="after")
    def _null_only_by_omission(self) -> "RefundRequest":
        if self.amount_minor is None and "amount_minor" in self.model_fields_set:
            raise ValueError("amount_minor must be omitted, not null, to refund the full amount")
        return self


DisputeState = Literal["opened", "under_review", "won", "lost"]


class DisputeView(BaseModel):
    dispute_id: uuid.UUID
    payment_id: uuid.UUID
    state: DisputeState
    amount_minor: int
    currency: str
    reason: str
    chargeback_fee_minor: int
    created_at: datetime
    updated_at: datetime


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)

    @field_validator("text")
    @classmethod
    def _printable_and_not_blank(cls, value: str) -> str:
        if any(char not in _EVIDENCE_ALLOWED_CONTROLS and _is_control_or_format(char) for char in value):
            raise ValueError("text must not contain control or format characters")
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


_EVIDENCE_ALLOWED_CONTROLS = frozenset("\n\r\t")


def _is_control_or_format(char: str) -> bool:
    """Unicode Cc (controls such as NUL) and Cf (zero-width spaces, bidi overrides)."""
    return unicodedata.category(char) in ("Cc", "Cf")
