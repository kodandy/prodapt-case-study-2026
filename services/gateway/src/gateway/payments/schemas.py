import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

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


class ChargeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount_minor: int = Field(gt=0)
    currency: Literal["USD", "EUR", "JPY", "INR"]
    method: Literal["card"] = "card"
    card: CardInput


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
