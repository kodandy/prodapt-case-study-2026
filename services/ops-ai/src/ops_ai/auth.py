import uuid
from datetime import datetime

import jwt

from ops_ai.scope import Scope

JWT_ISSUER = "gateway"
JWT_AUDIENCE = "ops-ai"
JWT_ALGORITHMS = ["RS256"]


class InvalidTokenError(Exception):
    pass


def verify_merchant_token(token: str, public_key_pem: str, now: datetime) -> Scope:
    """Verifies a gateway-issued merchant token and returns its scope. Any failure → InvalidTokenError.

    The algorithm list is pinned to RS256 (no `none`, no HMAC with the public key). Expiry is checked against
    the injected `now`, not the wall clock, so PyJWT's own time checks are off. Error messages never carry the
    token.
    """
    try:
        claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=JWT_ALGORITHMS,
            audience=JWT_AUDIENCE,
            issuer=JWT_ISSUER,
            options={
                "require": ["exp", "iss", "aud", "merchant_id"],
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
    merchant_id = claims["merchant_id"]
    if not isinstance(merchant_id, str):
        raise InvalidTokenError("invalid merchant_id claim")
    try:
        return Scope(merchant_id=uuid.UUID(merchant_id))
    except ValueError:
        raise InvalidTokenError("invalid merchant_id claim") from None
