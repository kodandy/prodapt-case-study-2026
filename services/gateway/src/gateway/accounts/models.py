import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Integer, LargeBinary, String, text
from sqlalchemy.orm import Mapped, mapped_column

from gateway.db import GatewayBase, uuid_pk


class User(GatewayBase):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime]


class Merchant(GatewayBase):
    __tablename__ = "merchants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime]


class ApiKey(GatewayBase):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = uuid_pk()
    merchant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("merchants.id"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    prefix: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime]


class UserTotp(GatewayBase):
    """`confirmed_at` set = TOTP enabled. The secret is AES-256-GCM ciphertext (`gateway.accounts.mfa`)."""

    __tablename__ = "user_totp"
    __table_args__ = (CheckConstraint("failed_attempts >= 0", name="failed_attempts_non_negative"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    secret_ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    confirmed_at: Mapped[datetime | None]
    last_used_step: Mapped[int | None] = mapped_column(BigInteger)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class LoginThrottle(GatewayBase):
    """Per-account password-failure counter (spec B1 Decision 7, `gateway.accounts.throttle`)."""

    __tablename__ = "login_throttle"
    __table_args__ = (CheckConstraint("failed_attempts >= 0", name="failed_attempts_non_negative"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    window_started_at: Mapped[datetime | None]
    locked_until: Mapped[datetime | None]
    updated_at: Mapped[datetime]
