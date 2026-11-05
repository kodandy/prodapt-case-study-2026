"""The single ops-scope check for the cross-merchant analytics routes (docs/specs/analytics.md section 4)."""

from dataclasses import dataclass

from ops_ai.scope import ForbiddenPrincipalError, Principal, require_ops_role


@dataclass(frozen=True)
class OpsAccess:
    actor: str


class ForbiddenOpsError(Exception):
    pass


def require_ops_principal(principal: Principal) -> OpsAccess:
    """An ops staff principal (ops_agent / ops_lead) → its actor; any other principal → ForbiddenOpsError."""
    try:
        ops = require_ops_role(principal)
    except ForbiddenPrincipalError:
        raise ForbiddenOpsError(type(principal).__name__) from None
    return OpsAccess(actor=f"ops:{ops.ops_user_id}")
