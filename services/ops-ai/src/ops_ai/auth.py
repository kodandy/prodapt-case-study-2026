import uuid
from datetime import datetime

import jwt

from ops_ai.scope import Principal, Scope, UserScope

JWT_ISSUER = "gateway"
JWT_AUDIENCE = "ops-ai"
JWT_ALGORITHMS = ["RS256"]
MERCHANT_ROLES = ["merchant_admin"]
USER_ROLES = ["consumer"]


class InvalidTokenError(Exception):
    pass


def verify_token(token: str, public_key_pem: str, now: datetime) -> Principal:
    """Verifies a gateway-issued token and returns its principal. Any failure → InvalidTokenError.

    The algorithm list is pinned to RS256 (no `none`, no HMAC with the public key). Expiry is checked against
    the injected `now`, not the wall clock, so PyJWT's own time checks are off. A token carries exactly one of
    `merchant_id` / `user_id`, with the matching role list. Error messages never carry the token.
    """
    try:
        claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=JWT_ALGORITHMS,
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "require": ["exp", "iss", "aud"],
                "verify_exp": False,
                "verify_iat": False,
                "verify_nbf": False,
            },
        )
    except jwt.PyJWTError:
        raise InvalidTokenError("invalid token") from None
    expires_at = claims["exp"]
    if not isinstance(expires_at, int) or isinstance(expires_at, bool) or now.timestamp() >= expires_at:
        raise InvalidTokenError("token expired")
    has_merchant, has_user = "merchant_id" in claims, "user_id" in claims
    if has_merchant == has_user:
        raise InvalidTokenError("token needs exactly one of merchant_id and user_id")
    if has_merchant:
        _require_roles(claims, MERCHANT_ROLES)
        return Scope(merchant_id=_uuid_claim(claims, "merchant_id"))
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


def _require_roles(claims: dict[str, object], expected: list[str]) -> None:
    roles = claims.get("roles")
    if not isinstance(roles, list) or roles != expected:
        raise InvalidTokenError("invalid roles claim")


def _uuid_claim(claims: dict[str, object], name: str) -> uuid.UUID:
    value = claims[name]
    if not isinstance(value, str) or not value:
        raise InvalidTokenError(f"invalid {name} claim")
    try:
        return uuid.UUID(value)
    except ValueError:
        raise InvalidTokenError(f"invalid {name} claim") from None
