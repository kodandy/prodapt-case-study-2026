import uuid
from datetime import datetime

import jwt

TOKEN_ISSUER = "gateway"  # noqa: S105 - JWT claim/header value, not a secret
TOKEN_AUDIENCE = "ops-ai"  # noqa: S105 - JWT claim/header value, not a secret
TOKEN_ALGORITHM = "RS256"  # noqa: S105 - JWT claim/header value, not a secret
MERCHANT_TOKEN_TTL_SECONDS = 900
USER_TOKEN_TTL_SECONDS = 900
OPS_TOKEN_TTL_SECONDS = 900


def issue_merchant_token(
    private_key_pem: str,
    merchant_id: uuid.UUID,
    now: datetime,
    ttl_seconds: int = MERCHANT_TOKEN_TTL_SECONDS,
    role: str = "merchant_admin",
) -> str:
    """Short-lived RS256 access token that ops-ai verifies with the public key; scope = `merchant_id` only."""
    issued_at = int(now.timestamp())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": [role],
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm=TOKEN_ALGORITHM)


def issue_user_token(
    private_key_pem: str, user_id: uuid.UUID, now: datetime, ttl_seconds: int = USER_TOKEN_TTL_SECONDS
) -> str:
    """Short-lived RS256 access token for ops-ai; scope = `user_id` only, no merchant claim."""
    issued_at = int(now.timestamp())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "sub": f"user:{user_id}",
        "user_id": str(user_id),
        "roles": ["consumer"],
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm=TOKEN_ALGORITHM)


def issue_ops_token(
    private_key_pem: str,
    ops_user_id: uuid.UUID,
    role: str,
    now: datetime,
    ttl_seconds: int = OPS_TOKEN_TTL_SECONDS,
) -> str:
    """Short-lived RS256 access token for ops-ai; scope = one ops principal and its single role."""
    issued_at = int(now.timestamp())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "sub": f"ops:{ops_user_id}",
        "ops_user_id": str(ops_user_id),
        "roles": [role],
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm=TOKEN_ALGORITHM)
