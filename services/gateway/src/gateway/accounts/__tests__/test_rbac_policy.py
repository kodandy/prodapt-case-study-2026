"""Tests for gateway.accounts.rbac, written from docs/specs/compliance-rbac.md section 1."""

import pytest
from gateway.accounts.rbac import POLICY, PUBLIC, ROLES, ROUTE_ACTIONS, action_for, is_allowed

CONSUMER = frozenset({"consumer"})
MERCHANT_BOTH = frozenset({"merchant_admin", "merchant_support"})
MERCHANT_ADMIN = frozenset({"merchant_admin"})
OPS_BOTH = frozenset({"ops_agent", "ops_lead"})
OPS_LEAD = frozenset({"ops_lead"})

EXPECTED_POLICY: dict[str, frozenset[str]] = {
    "me.mfa": CONSUMER,
    "me.wallet.read": CONSUMER,
    "me.transfer.read": CONSUMER,
    "me.transfer.create": CONSUMER,
    "me.profile.read": CONSUMER,
    "me.profile.rectify": CONSUMER,
    "me.data.export": CONSUMER,
    "me.data.erase": CONSUMER,
    "me.data_requests.read": CONSUMER,
    "merchant.token.issue": MERCHANT_BOTH,
    "payment.create": MERCHANT_ADMIN,
    "payment.read": MERCHANT_BOTH,
    "refund.create": MERCHANT_ADMIN,
    "merchant.dispute.read": MERCHANT_BOTH,
    "merchant.dispute.evidence": MERCHANT_BOTH,
    "dispute.read": OPS_BOTH,
    "dispute.resolve": OPS_LEAD,
    "ops.token.issue": OPS_BOTH,
    "kyc.review.read": OPS_BOTH,
    "kyc.verify": OPS_BOTH,
    "dsr.read": OPS_BOTH,
    "audit.read": OPS_LEAD,
}

EXPECTED_ROUTES: dict[str, str] = {
    "GET /healthz": "public",
    "GET /readyz": "public",
    "POST /v1/signup": "public",
    "POST /v1/sessions": "public",
    "POST /v1/user-tokens": "public",
    "POST /v1/ops/sessions": "public",
    "POST /v1/me/mfa/totp": "me.mfa",
    "POST /v1/me/mfa/totp/verify": "me.mfa",
    "GET /v1/me/wallets": "me.wallet.read",
    "GET /v1/me/transfers": "me.transfer.read",
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

ALL_ROLES = ("consumer", "merchant_admin", "merchant_support", "ops_agent", "ops_lead")
IS_ALLOWED_CASES = [
    (role, action, role in roles) for action, roles in EXPECTED_POLICY.items() for role in ALL_ROLES
]


def test_roles_is_the_five_roles_in_spec_order() -> None:
    # Arrange
    expected = ("consumer", "merchant_admin", "merchant_support", "ops_agent", "ops_lead")

    # Act
    roles = ROLES

    # Assert
    assert roles == expected


def test_public_marker_is_the_string_public() -> None:
    # Arrange / Act
    marker = PUBLIC

    # Assert
    assert marker == "public"


def test_policy_rows_match_the_spec_table_exactly() -> None:
    # Arrange
    expected = EXPECTED_POLICY

    # Act
    actual = {action: frozenset(roles) for action, roles in POLICY.items() if action != PUBLIC}

    # Assert
    assert actual == expected


@pytest.mark.parametrize(("role", "action", "allowed"), IS_ALLOWED_CASES)
def test_is_allowed_follows_the_policy_table(role: str, action: str, allowed: bool) -> None:
    # Arrange / Act
    result = is_allowed(role, action)

    # Assert
    assert result is allowed


@pytest.mark.parametrize("role", ["root", "", "ops_admin", "OPS_LEAD", "ops-lead", "merchant"])
def test_is_allowed_denies_an_unknown_role_for_every_known_action(role: str) -> None:
    # Arrange
    actions = sorted(EXPECTED_POLICY)

    # Act
    results = {action: is_allowed(role, action) for action in actions}

    # Assert
    assert results == dict.fromkeys(actions, False)


@pytest.mark.parametrize("action", ["", "payment.delete", "unknown", "payment.create ", "PAYMENT.CREATE"])
def test_is_allowed_denies_an_unknown_action_for_every_known_role(action: str) -> None:
    # Arrange / Act
    results = {role: is_allowed(role, action) for role in ALL_ROLES}

    # Assert
    assert results == dict.fromkeys(ALL_ROLES, False)


def test_is_allowed_denies_an_unknown_role_on_an_unknown_action() -> None:
    # Arrange / Act
    result = is_allowed("root", "everything")

    # Assert
    assert result is False


@pytest.mark.parametrize("action", sorted(EXPECTED_POLICY))
def test_every_action_belongs_to_exactly_one_principal_family(action: str) -> None:
    # Arrange
    families = (CONSUMER, MERCHANT_BOTH, OPS_BOTH)
    allowed = frozenset(role for role in ALL_ROLES if is_allowed(role, action))

    # Act
    containing = [family for family in families if allowed <= family]

    # Assert
    assert len(containing) == 1
    assert allowed != frozenset()


@pytest.mark.parametrize("ops_role", ["ops_agent", "ops_lead"])
def test_ops_roles_are_never_allowed_a_consumer_or_merchant_action(ops_role: str) -> None:
    # Arrange
    non_ops_actions = [action for action, roles in EXPECTED_POLICY.items() if not roles <= OPS_BOTH]

    # Act
    granted = [action for action in non_ops_actions if is_allowed(ops_role, action)]

    # Assert
    assert granted == []


@pytest.mark.parametrize("role", ["consumer", "merchant_admin", "merchant_support"])
def test_consumer_and_merchant_roles_are_never_allowed_an_ops_action(role: str) -> None:
    # Arrange
    ops_actions = [action for action, roles in EXPECTED_POLICY.items() if roles <= OPS_BOTH]

    # Act
    granted = [action for action in ops_actions if is_allowed(role, action)]

    # Assert
    assert granted == []


def test_merchant_support_can_read_payments_and_issue_tokens_but_not_create_or_refund() -> None:
    # Arrange
    support = "merchant_support"

    # Act
    results = {
        action: is_allowed(support, action)
        for action in ("payment.read", "payment.create", "refund.create", "merchant.token.issue")
    }

    # Assert
    assert results == {
        "payment.read": True,
        "payment.create": False,
        "refund.create": False,
        "merchant.token.issue": True,
    }


def test_ops_agent_cannot_read_the_audit_log_but_ops_lead_can() -> None:
    # Arrange / Act
    results = (is_allowed("ops_agent", "audit.read"), is_allowed("ops_lead", "audit.read"))

    # Assert
    assert results == (False, True)


@pytest.mark.parametrize(("key", "action"), sorted(EXPECTED_ROUTES.items()))
def test_route_actions_maps_each_spec_route_to_its_action(key: str, action: str) -> None:
    # Arrange / Act
    mapped = ROUTE_ACTIONS.get(key)

    # Assert
    assert mapped == action


@pytest.mark.parametrize(("key", "action"), sorted(EXPECTED_ROUTES.items()))
def test_action_for_returns_the_mapped_action(key: str, action: str) -> None:
    # Arrange
    method, path = key.split(" ", 1)

    # Act
    result = action_for(method, path)

    # Assert
    assert result == action


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("DELETE", "/v1/payments"),
        ("GET", "/v1/nonexistent"),
        ("PUT", "/v1/me/profile"),
        ("GET", "/v1/payments/123"),
        ("GET", ""),
        ("", "/v1/payments"),
    ],
)
def test_action_for_returns_none_for_an_unmapped_route(method: str, path: str) -> None:
    # Arrange / Act
    result = action_for(method, path)

    # Assert
    assert result is None


def test_every_non_public_route_action_is_a_policy_key() -> None:
    # Arrange
    used = {action for action in ROUTE_ACTIONS.values() if action != PUBLIC}

    # Act
    missing = sorted(used - set(POLICY))

    # Assert
    assert missing == []
