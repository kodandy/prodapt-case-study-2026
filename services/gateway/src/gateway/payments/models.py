import uuid
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk


class Payment(GatewayBase):
    """state: created | succeeded | declined | failed | unknown."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(16))
    card_token: Mapped[str | None] = mapped_column(ForeignKey("card_tokens.token"))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    entity_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class PaymentAttempt(GatewayBase):
    """The attempt id is the provider idempotency key."""

    __tablename__ = "payment_attempts"

    id: Mapped[uuid.UUID] = uuid_pk()
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    route_config_version: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    provider_ref: Mapped[str | None] = mapped_column(String(64))
    decline_code: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
