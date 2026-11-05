"""Role x action policy (P0-9). Every route maps to one action; every credential dependency checks it."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, get_args

Role = Literal["consumer", "merchant_admin", "merchant_support", "ops_agent", "ops_lead"]
MerchantRole = Literal["merchant_admin", "merchant_support"]
OpsRole = Literal["ops_agent", "ops_lead"]

ROLES: tuple[Role, ...] = get_args(Role)
MERCHANT_ROLES: tuple[MerchantRole, ...] = get_args(MerchantRole)
OPS_ROLES: tuple[OpsRole, ...] = get_args(OpsRole)
PUBLIC = "public"

_CONSUMER: frozenset[Role] = frozenset({"consumer"})
_MERCHANT_ANY: frozenset[Role] = frozenset({"merchant_admin", "merchant_support"})
_MERCHANT_ADMIN: frozenset[Role] = frozenset({"merchant_admin"})
_OPS_ANY: frozenset[Role] = frozenset({"ops_agent", "ops_lead"})
_OPS_LEAD: frozenset[Role] = frozenset({"ops_lead"})

POLICY: Mapping[str, frozenset[Role]] = MappingProxyType(
    {
        "me.mfa": _CONSUMER,
        "me.wallet.read": _CONSUMER,
        "me.transfer.read": _CONSUMER,
        "me.transfer.create": _CONSUMER,
        "me.profile.read": _CONSUMER,
        "me.profile.rectify": _CONSUMER,
        "me.data.export": _CONSUMER,
        "me.data.erase": _CONSUMER,
        "me.data_requests.read": _CONSUMER,
        "merchant.token.issue": _MERCHANT_ANY,
        "payment.create": _MERCHANT_ADMIN,
        "payment.read": _MERCHANT_ANY,
        "refund.create": _MERCHANT_ADMIN,
        "merchant.dispute.read": _MERCHANT_ANY,
        "merchant.dispute.evidence": _MERCHANT_ANY,
        "dispute.read": _OPS_ANY,
        "dispute.resolve": _OPS_LEAD,
        "ops.token.issue": _OPS_ANY,
        "kyc.review.read": _OPS_ANY,
        "kyc.verify": _OPS_ANY,
        "dsr.read": _OPS_ANY,
        "audit.read": _OPS_LEAD,
    }
)

ROUTE_ACTIONS: Mapping[str, str] = MappingProxyType(
    {
        "GET /healthz": PUBLIC,
        "GET /readyz": PUBLIC,
        "GET /metrics": PUBLIC,
        "POST /v1/signup": PUBLIC,
        "POST /v1/sessions": PUBLIC,
        "POST /v1/user-tokens": PUBLIC,
        "POST /v1/ops/sessions": PUBLIC,
        "POST /v1/me/mfa/totp": "me.mfa",
        "POST /v1/me/mfa/totp/verify": "me.mfa",
        "GET /v1/me/wallets": "me.wallet.read",
        "GET /v1/me/transfers": "me.transfer.read",
        "GET /v1/me/transactions": "me.transfer.read",
        "POST /v1/me/transfers": "me.transfer.create",
        "GET /v1/me/profile": "me.profile.read",
        "PATCH /v1/me/profile": "me.profile.rectify",
        "POST /v1/me/data-requests/export": "me.data.export",
        "POST /v1/me/data-requests/erasure": "me.data.erase",
        "GET /v1/me/data-requests": "me.data_requests.read",
        "POST /v1/merchant-tokens": "merchant.token.issue",
        "POST /v1/payments": "payment.create",
        "GET /v1/payments": "payment.read",
        "GET /v1/payments/{payment_id}": "payment.read",
        "GET /v1/payments/{payment_id}/risk": "payment.read",
        "POST /v1/payments/{payment_id}/refunds": "refund.create",
        "GET /v1/disputes/{dispute_id}": "merchant.dispute.read",
        "POST /v1/disputes/{dispute_id}/evidence": "merchant.dispute.evidence",
        "POST /v1/ops/ops-ai-tokens": "ops.token.issue",
        "GET /v1/ops/kyc-reviews": "kyc.review.read",
        "POST /v1/ops/users/{user_id}/kyc/verify": "kyc.verify",
        "GET /v1/ops/data-requests": "dsr.read",
        "GET /v1/ops/audit": "audit.read",
    }
)


def is_allowed(role: str, action: str) -> bool:
    """Unknown role or action → False (default deny)."""
    allowed = POLICY.get(action)
    return allowed is not None and role in allowed


def action_for(method: str, path_template: str) -> str | None:
    return ROUTE_ACTIONS.get(f"{method.upper()} {path_template}")
