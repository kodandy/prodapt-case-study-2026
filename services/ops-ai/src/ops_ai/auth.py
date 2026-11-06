import uuid
from datetime import datetime
from typing import TypeGuard

import jwt

from ops_ai.scope import MERCHANT_ROLES, OPS_ROLES, OpsScope, Principal, Scope, UserScope

JWT_ISSUER = "gateway"
JWT_AUDIENCE = "ops-ai"
JWT_ALGORITHMS = ["RS256"]
USER_ROLES = ["consumer"]
MAX_CLOCK_SKEW_SECONDS = 30
MAX_TOKEN_LIFETIME_SECONDS = 900


class InvalidTokenError(Exception):
    pass


def verify_token(token: str, public_key_pem: str, now: datetime) -> Principal:
    """Verifies a gateway-issued token and returns its principal. Any failure → InvalidTokenError.

    The algorithm list is pinned to RS256 (no `none`, no HMAC with the public key). Expiry is checked against
    the injected `now`, not the wall clock, so PyJWT's own time checks are off. `iat` is required, may lie
    at most MAX_CLOCK_SKEW_SECONDS ahead of `now`, and `exp - iat` is capped at MAX_TOKEN_LIFETIME_SECONDS.
    A token carries exactly one of `merchant_id` / `user_id` / `ops_user_id` (canonical UUID string) and a
    one-element role list of that kind. User and ops tokens must also carry the matching `sub`. A merchant
    token may carry `api_key_id` (canonical UUID string), the gateway key that obtained it; any other token
    carrying it is refused.
    Error messages never carry the token.
    """
    try:
        claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=JWT_ALGORITHMS,
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "require": ["exp", "iat", "iss", "aud"],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError:
        raise InvalidTokenError("invalid token") from None
    expires_at = claims["exp"]
    if not _is_int(expires_at) or now.timestamp() >= expires_at:
        raise InvalidTokenError("token expired")
    issued_at = claims["iat"]
    if not _is_int(issued_at):
        raise InvalidTokenError("invalid iat claim")
    if issued_at > now.timestamp() + MAX_CLOCK_SKEW_SECONDS:
        raise InvalidTokenError("token issued in the future")
    if expires_at - issued_at > MAX_TOKEN_LIFETIME_SECONDS:
        raise InvalidTokenError("token lifetime too long")
    kinds = [name for name in ("merchant_id", "user_id", "ops_user_id") if name in claims]
    if len(kinds) != 1:
        raise InvalidTokenError("token needs exactly one of merchant_id, user_id and ops_user_id")
    if kinds != ["merchant_id"] and "api_key_id" in claims:
        raise InvalidTokenError("api_key_id is only valid on a merchant token")
    if kinds == ["merchant_id"]:
        merchant_role = _single_role(claims, MERCHANT_ROLES)
        api_key_id = _uuid_claim(claims, "api_key_id") if "api_key_id" in claims else None
        return Scope(
            merchant_id=_uuid_claim(claims, "merchant_id"), role=merchant_role, api_key_id=api_key_id
        )
    if kinds == ["ops_user_id"]:
        ops_role = _single_role(claims, OPS_ROLES)
        ops_user_id = _uuid_claim(claims, "ops_user_id")
        if claims.get("sub") != f"ops:{ops_user_id}":
            raise InvalidTokenError("invalid sub claim")
        return OpsScope(ops_user_id=ops_user_id, role=ops_role)
    _require_roles(claims, USER_ROLES)
    user_id = _uuid_claim(claims, "user_id")
    if claims.get("sub") != f"user:{user_id}":
        raise InvalidTokenError("invalid sub claim")
    return UserScope(user_id=user_id)


def verify_merchant_token(token: str, public_key_pem: str, now: datetime) -> Scope:
    """`verify_token`, restricted to merchant tokens."""
    principal = verify_token(token, public_key_pem, now)
    if not isinstance(principal, Scope):
        raise InvalidTokenError("not a merchant token")
    return principal


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_roles(claims: dict[str, object], expected: list[str]) -> None:
    roles = claims.get("roles")
    if not isinstance(roles, list) or roles != expected:
        raise InvalidTokenError("invalid roles claim")


def _single_role[RoleT: str](claims: dict[str, object], allowed: tuple[RoleT, ...]) -> RoleT:
    roles = claims.get("roles")
    if not isinstance(roles, list) or len(roles) != 1:
        raise InvalidTokenError("invalid roles claim")
    for role in allowed:
        if roles[0] == role:
            return role
    raise InvalidTokenError("invalid roles claim")


def _uuid_claim(claims: dict[str, object], name: str) -> uuid.UUID:
    value = claims[name]
    if not isinstance(value, str) or not value:
        raise InvalidTokenError(f"invalid {name} claim")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise InvalidTokenError(f"invalid {name} claim") from None
    if str(parsed) != value:
        raise InvalidTokenError(f"invalid {name} claim")
    return parsed
