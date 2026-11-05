import uuid
from dataclasses import dataclass
from typing import Literal

MerchantRole = Literal["merchant_admin", "merchant_support"]
OpsRole = Literal["ops_agent", "ops_lead"]
MERCHANT_ROLES: tuple[MerchantRole, ...] = ("merchant_admin", "merchant_support")
OPS_ROLES: tuple[OpsRole, ...] = ("ops_agent", "ops_lead")


@dataclass(frozen=True)
class Scope:
    """Built from auth only, never from model output. Every repository function takes one."""

    merchant_id: uuid.UUID
    role: MerchantRole = "merchant_admin"


@dataclass(frozen=True)
class UserScope:
    """A consumer's own data only. Built from a verified user token, never from the request body."""

    user_id: uuid.UUID


@dataclass(frozen=True)
class OpsScope:
    """An ops staff principal (P0-9). Cross-tenant by design, so it never reaches a merchant or user route."""

    ops_user_id: uuid.UUID
    role: OpsRole


Principal = Scope | UserScope | OpsScope


class ForbiddenPrincipalError(Exception):
    pass


def require_ops_role(principal: Principal, allowed: frozenset[str] = frozenset(OPS_ROLES)) -> OpsScope:
    """The ops principal when its role is in `allowed`; any other principal → ForbiddenPrincipalError."""
    if not isinstance(principal, OpsScope) or principal.role not in allowed:
        raise ForbiddenPrincipalError
    return principal
