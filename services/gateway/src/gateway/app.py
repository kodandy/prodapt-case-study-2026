import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gateway.accounts.service import (
    EmailAlreadyRegisteredError,
    authenticate_api_key,
    authenticate_user,
    sign_up,
)
from gateway.accounts.tokens import (
    MERCHANT_TOKEN_TTL_SECONDS,
    USER_TOKEN_TTL_SECONDS,
    issue_merchant_token,
    issue_user_token,
)
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.schemas import ChargeRequest


@dataclass(frozen=True)
class GatewayAppDeps:
    charge: ChargeDeps
    api_key_pepper: str = field(repr=False)
    jwt_private_key_pem: str | None = field(default=None, repr=False)
    readiness: Callable[[], list[str]] = field(default=lambda: [])


class SignUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    password: SecretStr = Field(min_length=8, max_length=256)


class UserTokenRequest(SignUpRequest):
    """Same field rules as sign-up."""


class SignUpResponse(BaseModel):
    user_id: str
    wallet_account_id: str


class MerchantTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type label, not a secret
    expires_in: int


class UserTokenResponse(MerchantTokenResponse):
    pass


def create_app(deps: GatewayAppDeps) -> FastAPI:
    """Routes:
    - POST /v1/signup {email, password} → 201 {user_id, wallet_account_id}; duplicate email → 409.
    - POST /v1/payments (Authorization: Bearer <api key>, Idempotency-Key header required)
      body ChargeRequest → ChargeOutcome status/body. Missing/invalid API key → 401;
      missing Idempotency-Key → 400; invalid card → 422.
    - POST /v1/merchant-tokens (Bearer API key) → 201 {access_token, token_type, expires_in}: an RS256 token
      for ops-ai. Invalid API key → 401; no private key configured → 503.
    - POST /v1/user-tokens {email, password} → 201 {access_token, token_type, expires_in}: an RS256 user token
      for ops-ai. Unknown email and wrong password → the identical 401; no private key configured → 503.
    - GET /healthz → 200 (liveness). GET /readyz → 200 ready, or 503 with the failed components.
    """
    app = FastAPI(title="gateway")
    app.add_exception_handler(RequestValidationError, _validation_error_without_input)
    bearer = HTTPBearer(auto_error=False)
    session_factory = deps.charge.session_factory

    def merchant_from_api_key(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> uuid.UUID:
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing api key")
        with session_factory() as session:
            merchant_id = authenticate_api_key(session, credentials.credentials, deps.api_key_pepper)
        if merchant_id is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
        return merchant_id

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        failed = deps.readiness()
        if failed:
            return JSONResponse({"status": "not_ready", "failed": failed}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.post("/v1/merchant-tokens", status_code=status.HTTP_201_CREATED)
    def merchant_tokens(
        merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)],
        response: Response,
    ) -> MerchantTokenResponse:
        if deps.jwt_private_key_pem is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "token issuing not configured")
        token = issue_merchant_token(deps.jwt_private_key_pem, merchant_id, deps.charge.clock())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return MerchantTokenResponse(access_token=token, expires_in=MERCHANT_TOKEN_TTL_SECONDS)

    @app.post("/v1/user-tokens", status_code=status.HTTP_201_CREATED)
    def user_tokens(body: UserTokenRequest, response: Response) -> UserTokenResponse:
        if deps.jwt_private_key_pem is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "token issuing not configured")
        with session_factory() as session:
            user_id = authenticate_user(session, body.email, body.password.get_secret_value())
        if user_id is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
        token = issue_user_token(deps.jwt_private_key_pem, user_id, deps.charge.clock())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return UserTokenResponse(access_token=token, expires_in=USER_TOKEN_TTL_SECONDS)

    @app.post("/v1/signup", status_code=status.HTTP_201_CREATED)
    def signup(body: SignUpRequest) -> SignUpResponse:
        try:
            with session_factory.begin() as session:
                result = sign_up(session, deps.charge.clock, body.email, body.password.get_secret_value())
        except EmailAlreadyRegisteredError:
            raise HTTPException(status.HTTP_409_CONFLICT, "email already registered") from None
        return SignUpResponse(user_id=str(result.user_id), wallet_account_id=str(result.wallet_account_id))

    @app.post("/v1/payments")
    def create_payment(
        body: ChargeRequest,
        merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)],
        idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
    ) -> JSONResponse:
        if not idempotency_key:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Idempotency-Key header is required")
        outcome = charge_card(deps.charge, merchant_id, idempotency_key, body)
        return JSONResponse(outcome.body, status_code=outcome.status_code)

    return app


async def _validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's default 422 echoes the rejected input; for a card that would be the PAN."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in errors]
    return JSONResponse({"error": "validation_failed", "detail": detail}, status_code=422)
