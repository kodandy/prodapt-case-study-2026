import uuid
from dataclasses import dataclass
from datetime import datetime

import jwt
from cryptography.hazmat.primitives import serialization

from gateway.accounts.tokens import TOKEN_ALGORITHM, TOKEN_ISSUER

SESSION_AUDIENCE = "gateway"
SESSION_TOKEN_TTL_SECONDS = 900
AMR_FIRST_FACTOR = "pwd"
AMR_OTP = "otp"
_REQUIRED_CLAIMS = ["iss", "aud", "sub", "exp", "iat", "jti"]


@dataclass(frozen=True)
class SessionClaims:
    user_id: uuid.UUID
    amr: tuple[str, ...]


class InvalidSessionError(Exception):
    pass


def issue_session_token(private_key_pem: str, user_id: uuid.UUID, now: datetime, amr: tuple[str, ...]) -> str:
    """Gateway-audience RS256 session; distinct from the ops-ai tokens in `tokens.py`."""
    issued_at = int(now.timestamp())
    claims = {
        "iss": TOKEN_ISSUER,
        "aud": SESSION_AUDIENCE,
        "sub": f"user:{user_id}",
        "user_id": str(user_id),
        "amr": list(amr),
        "iat": issued_at,
        "exp": issued_at + SESSION_TOKEN_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_key_pem, algorithm=TOKEN_ALGORITHM)


def verify_session_token(private_key_pem: str, token: str, now: datetime) -> SessionClaims:
    """Raises InvalidSessionError for any token that is not a live gateway session. Expiry is checked against
    the injected `now`, not the wall clock."""
    try:
        claims = jwt.decode(
            token,
            _public_key_pem(private_key_pem),
            algorithms=[TOKEN_ALGORITHM],
            audience=SESSION_AUDIENCE,
            issuer=TOKEN_ISSUER,
            options={"require": _REQUIRED_CLAIMS, "verify_exp": False, "verify_iat": False},
        )
    except jwt.PyJWTError as exc:
        raise InvalidSessionError from exc
    expires_at = claims["exp"]
    if not isinstance(expires_at, int) or expires_at <= int(now.timestamp()):
        raise InvalidSessionError
    user_id = claims.get("user_id")
    amr = claims.get("amr")
    if not isinstance(user_id, str) or not isinstance(amr, list) or not all(isinstance(m, str) for m in amr):
        raise InvalidSessionError
    if claims["sub"] != f"user:{user_id}":
        raise InvalidSessionError
    try:
        parsed = uuid.UUID(user_id)
    except ValueError as exc:
        raise InvalidSessionError from exc
    return SessionClaims(user_id=parsed, amr=tuple(amr))


def _public_key_pem(private_key_pem: str) -> bytes:
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
