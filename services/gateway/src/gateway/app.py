import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gateway.accounts.service import EmailAlreadyRegisteredError, authenticate_api_key, sign_up
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.schemas import ChargeRequest


@dataclass(frozen=True)
class GatewayAppDeps:
    charge: ChargeDeps
    api_key_pepper: str


class SignUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    password: SecretStr = Field(min_length=8, max_length=256)


class SignUpResponse(BaseModel):
    user_id: str
    wallet_account_id: str


def create_app(deps: GatewayAppDeps) -> FastAPI:
    """Routes:
    - POST /v1/signup {email, password} → 201 {user_id, wallet_account_id}; duplicate email → 409.
    - POST /v1/payments (Authorization: Bearer <api key>, Idempotency-Key header required)
      body ChargeRequest → ChargeOutcome status/body. Missing/invalid API key → 401;
      missing Idempotency-Key → 400; invalid card → 422.
    - GET /healthz → 200.
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
