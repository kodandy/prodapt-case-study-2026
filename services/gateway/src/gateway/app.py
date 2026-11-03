import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from gateway.accounts.service import (
    EmailAlreadyRegisteredError,
    authenticate_api_key,
    find_login,
    sign_up,
    verify_login,
)
from gateway.accounts.tokens import (
    MERCHANT_TOKEN_TTL_SECONDS,
    USER_TOKEN_TTL_SECONDS,
    issue_merchant_token,
    issue_user_token,
)
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import (
    DisputeNotFoundError,
    InvalidDisputeTransitionError,
    get_dispute,
    submit_evidence,
)
from gateway.payments.history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, InvalidCursorError, list_payments
from gateway.payments.refunds import refund_payment
from gateway.payments.schemas import (
    ChargeRequest,
    DisputeView,
    EvidenceRequest,
    PaymentSummary,
    RefundRequest,
)
from gateway.payments.state import PaymentState

LOGIN_CONCURRENCY = 4
LOGIN_SLOT_WAIT_SECONDS = 2.0
IDEMPOTENCY_KEY_PATTERN = re.compile(r"[\x21-\x7e]{1,255}")


@dataclass(frozen=True)
class GatewayAppDeps:
    charge: ChargeDeps
    api_key_pepper: str = field(repr=False)
    jwt_private_key_pem: str | None = field(default=None, repr=False)
    readiness: Callable[[], list[str]] = field(default=lambda: [])
    login_slots: threading.BoundedSemaphore | None = None
    """Caps concurrent argon2 verifications on login; None → LOGIN_CONCURRENCY slots."""


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


class PaymentListResponse(BaseModel):
    items: list[PaymentSummary]
    next_cursor: str | None


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
      At most LOGIN_CONCURRENCY password checks run at once; no slot within LOGIN_SLOT_WAIT_SECONDS → 503
      {"detail": "try again later"} with Retry-After: 1.
    - GET /v1/payments?limit&cursor&state (Bearer API key) → 200 {items, next_cursor}: the merchant's
      payments, newest first. Invalid cursor → 400.
    - POST /v1/payments/{payment_id}/refunds (Bearer API key, Idempotency-Key required) body RefundRequest →
      RefundOutcome status/body. Idempotency-Key outside visible ASCII → 400; explicit null amount → 422.
    - GET /v1/disputes/{dispute_id}, POST /v1/disputes/{dispute_id}/evidence (Bearer API key) → DisputeView;
      unknown or another merchant's → 404; not `opened` for evidence → 409.
    - GET /healthz → 200 (liveness). GET /readyz → 200 ready, or 503 with the failed components.
    """
    app = FastAPI(title="gateway")
    app.add_exception_handler(RequestValidationError, _validation_error_without_input)
    bearer = HTTPBearer(auto_error=False)
    session_factory = deps.charge.session_factory
    login_slots = (
        threading.BoundedSemaphore(LOGIN_CONCURRENCY) if deps.login_slots is None else deps.login_slots
    )

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
            record = find_login(session, body.email)
        if not login_slots.acquire(timeout=LOGIN_SLOT_WAIT_SECONDS):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "try again later", headers={"Retry-After": "1"}
            )
        try:
            user_id = verify_login(record, body.password.get_secret_value())
        finally:
            login_slots.release()
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
        key = _require_idempotency_key(idempotency_key)
        outcome = charge_card(deps.charge, merchant_id, key, body)
        return JSONResponse(outcome.body, status_code=outcome.status_code)

    @app.get("/v1/payments")
    def payment_history(
        merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)],
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        state: PaymentState | None = None,
    ) -> PaymentListResponse:
        try:
            with session_factory() as session:
                page = list_payments(session, merchant_id, limit, cursor, state)
        except InvalidCursorError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
        return PaymentListResponse(items=page.items, next_cursor=page.next_cursor)

    @app.post("/v1/payments/{payment_id}/refunds")
    def create_refund(
        payment_id: uuid.UUID,
        merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)],
        _no_null_body: Annotated[None, Depends(_reject_null_body)],
        body: RefundRequest | None = None,
        idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
    ) -> JSONResponse:
        key = _require_idempotency_key(idempotency_key)
        outcome = refund_payment(
            session_factory,
            deps.charge.clock,
            merchant_id,
            payment_id,
            key,
            None if body is None else body.amount_minor,
        )
        return JSONResponse(outcome.body, status_code=outcome.status_code)

    @app.get("/v1/disputes/{dispute_id}")
    def read_dispute(
        dispute_id: uuid.UUID, merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)]
    ) -> DisputeView:
        try:
            with session_factory() as session:
                return get_dispute(session, merchant_id, dispute_id)
        except DisputeNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "dispute not found") from None

    @app.post("/v1/disputes/{dispute_id}/evidence")
    def dispute_evidence(
        dispute_id: uuid.UUID,
        body: EvidenceRequest,
        merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)],
    ) -> DisputeView:
        try:
            return submit_evidence(session_factory, deps.charge.clock, merchant_id, dispute_id, body.text)
        except DisputeNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "dispute not found") from None
        except InvalidDisputeTransitionError:
            raise HTTPException(status.HTTP_409_CONFLICT, "invalid dispute transition") from None

    return app


def _require_idempotency_key(idempotency_key: str | None) -> str:
    """Visible ASCII only: a NUL would otherwise reach Postgres and fail as a 500."""
    if not idempotency_key:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Idempotency-Key header is required")
    if IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid Idempotency-Key header")
    return idempotency_key


async def _reject_null_body(request: Request) -> None:
    """A JSON `null` body parses as an omitted body; for a refund that would mean "refund everything"."""
    if (await request.body()).strip() == b"null":
        raise RequestValidationError(
            [{"loc": ("body",), "msg": "body must be omitted, not null", "type": "null_body"}]
        )


async def _validation_error_without_input(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI's default 422 echoes the rejected input; for a card that would be the PAN."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    detail = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in errors]
    return JSONResponse({"error": "validation_failed", "detail": detail}, status_code=422)
