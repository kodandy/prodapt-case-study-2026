"""The single ops-scope check for the cross-merchant analytics routes.

There is no ops principal yet, so this is fail-closed. When the ops role lands (OpsScope with ops_agent /
ops_lead and scope.require_ops_role), only the body of require_ops_principal changes:
    return OpsAccess(actor=f"ops:{require_ops_role(principal).ops_user_id}")
"""

from dataclasses import dataclass

from ops_ai.scope import Principal


@dataclass(frozen=True)
class OpsAccess:
    actor: str


class ForbiddenOpsError(Exception):
    pass


def require_ops_principal(principal: Principal) -> OpsAccess:
    raise ForbiddenOpsError(type(principal).__name__)
