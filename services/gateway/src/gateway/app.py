import re
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictInt

from gateway.accounts.mfa import (
    InvalidTotpCodeError,
    TotpAlreadyEnabledError,
    TotpDecryptError,
    TotpLockedError,
    TotpNotEnrolledError,
    check_totp,
    confirm_totp,
    enrol_totp,
    totp_enabled,
)
from gateway.accounts.service import (
    EmailAlreadyRegisteredError,
    authenticate_api_key,
    find_login,
    sign_up,
    verify_login,
)
from gateway.accounts.sessions import (
    AMR_FIRST_FACTOR,
    AMR_OTP,
    SESSION_TOKEN_TTL_SECONDS,
    InvalidSessionError,
    issue_session_token,
    verify_session_token,
)
from gateway.accounts.throttle import (
    AccountLockedError,
    lock_retry_after,
    record_password_failure,
    settle_login_success,
)
from gateway.accounts.tokens import (
    MERCHANT_TOKEN_TTL_SECONDS,
    USER_TOKEN_TTL_SECONDS,
    issue_merchant_token,
    issue_user_token,
)
from gateway.audit.models import AuditLog
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import (
    DisputeNotFoundError,
    InvalidDisputeTransitionError,
    PaymentNotFoundError,
    get_dispute,
    submit_evidence,
)
from gateway.payments.history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, InvalidCursorError, list_payments
from gateway.payments.refunds import refund_payment
from gateway.payments.risk import get_payment_risk
from gateway.payments.schemas import (
    MAX_AMOUNT_MINOR,
    ChargeRequest,
    DisputeView,
    EvidenceRequest,
    PaymentRiskView,
    PaymentSummary,
    RefundRequest,
)
from gateway.payments.state import PaymentState
from gateway.wallets.history import TransferHistoryItem, list_transfers
from gateway.wallets.service import WalletBalanceView, list_wallets, transfer

LOGIN_CONCURRENCY = 4
NO_STORE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}
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
    totp_encryption_key: str | None = field(default=None, repr=False)


class SignUpRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    password: SecretStr = Field(min_length=8, max_length=256)


class SignUpResponse(BaseModel):
    user_id: str
    wallet_account_id: str


class MerchantTokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105 - OAuth token type label, not a secret
    expires_in: int


class UserTokenResponse(MerchantTokenResponse):
    pass


class SessionRequest(SignUpRequest):
    totp_code: SecretStr | None = Field(default=None, min_length=1, max_length=16)


class UserTokenRequest(SessionRequest):
    """Same field rules as a session login."""


class SessionResponse(MerchantTokenResponse):
    amr: list[str]


class TotpEnrolResponse(BaseModel):
    secret: str
    otpauth_uri: str


class TotpVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: SecretStr = Field(min_length=1, max_length=16)


class TotpVerifyResponse(BaseModel):
    totp_enabled: bool


class WalletListResponse(BaseModel):
    items: list[WalletBalanceView]


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipient_email: str = Field(min_length=1, max_length=320, pattern=r"^[^\x00]+$")
    """NUL would reach Postgres as a DataError (500)."""
    amount_minor: StrictInt = Field(gt=0, le=MAX_AMOUNT_MINOR)
    currency: Literal["USD", "EUR", "JPY", "INR"]


class PaymentListResponse(BaseModel):
    items: list[PaymentSummary]
    next_cursor: str | None


class TransferHistoryResponse(BaseModel):
    items: list[TransferHistoryItem]
    next_cursor: str | None


def create_app(deps: GatewayAppDeps) -> FastAPI:
    """Routes:
    - POST /v1/signup {email, password} → 201 {user_id, wallet_account_id}; duplicate email → 409.
    - POST /v1/payments (Authorization: Bearer <api key>, Idempotency-Key header required)
      body ChargeRequest → ChargeOutcome status/body. Missing/invalid API key → 401;
      missing Idempotency-Key → 400; invalid card → 422.
    - POST /v1/merchant-tokens (Bearer API key) → 201 {access_token, token_type, expires_in}: an RS256 token
      for ops-ai. Invalid API key → 401; no private key configured → 503.
    - POST /v1/user-tokens {email, password, totp_code?} → 201 {access_token, token_type, expires_in}: an
      RS256 user token for ops-ai. Unknown email and wrong password → the identical 401; no private key
      configured → 503. A user with TOTP enabled gets the same second-factor errors as /v1/sessions.
      At most LOGIN_CONCURRENCY password checks run at once; no slot within LOGIN_SLOT_WAIT_SECONDS → 503
      {"detail": "try again later"} with Retry-After: 1.
    - POST /v1/sessions {email, password, totp_code?} → 201 {access_token, token_type, expires_in, amr}: a
      gateway session (aud "gateway"). Bad credentials or a wrong TOTP code → 401 "invalid credentials"; TOTP
      enabled without a code → 401 "mfa required"; TOTP locked → 423; enabled without an encryption key → 503.
      Same login slots as /v1/user-tokens.
    - POST /v1/me/mfa/totp (Bearer session) → 201 {secret, otpauth_uri}; already enabled → 409.
    - POST /v1/me/mfa/totp/verify (Bearer session) {code} → 200 {"totp_enabled": true}; wrong code → 400;
      not enrolled → 404; already enabled → 409; locked → 423. Both MFA routes → 503 without the key.
    - GET /v1/me/wallets (Bearer session) → 200 {items}. Once TOTP is enabled, a `/v1/me/*` session must carry
      "otp" in amr, else 401 "mfa required".
    - POST /v1/me/transfers (Bearer session, Idempotency-Key required) body TransferRequest → TransferOutcome
      status/body (201 / 404 recipient_not_found / 409 insufficient_funds / 422 self_transfer or key
      reused), all with Cache-Control: no-store. Same currency only; FX is Design.
    - GET /v1/me/transfers?limit&cursor (Bearer session) → 200 {items, next_cursor}: the user's sent and
      received transfers, newest first, counterparty email masked; invalid cursor → 400. All with
      Cache-Control: no-store.
    - GET /v1/payments?limit&cursor&state (Bearer API key) → 200 {items, next_cursor}: the merchant's
      payments, newest first. Invalid cursor → 400.
    - GET /v1/payments/{payment_id}/risk (Bearer API key) → 200 PaymentRiskView (flags only); unknown or
      another merchant's → 404.
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

    def check_password(body: SignUpRequest) -> tuple[uuid.UUID, bool]:
        """The user id and whether TOTP is enabled, on a password success. Unknown email and wrong password
        take the same argon2 path and the identical 401; a known user's failure is audited and counted. A
        locked account (Decision 7) gets 429 before any slot or argon2 work, again once it holds a slot, and
        after argon2 if another request locked it meanwhile. 503 when no login slot is free."""
        with session_factory() as session:
            record = find_login(session, body.email)
            now = deps.charge.clock()
            locked = None if record is None else lock_retry_after(session, record.user_id, now)
        if locked is not None:
            raise too_many_attempts(locked)
        if not login_slots.acquire(timeout=LOGIN_SLOT_WAIT_SECONDS):
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "try again later", headers={"Retry-After": "1"}
            )
        try:
            if record is not None:
                with session_factory() as session:
                    locked = lock_retry_after(session, record.user_id, deps.charge.clock())
                if locked is not None:
                    raise too_many_attempts(locked)
            user_id = verify_login(record, body.password.get_secret_value())
        finally:
            login_slots.release()
        try:
            if user_id is None:
                if record is not None:
                    with session_factory.begin() as session:
                        record_password_failure(session, record.user_id, deps.charge.clock())
                raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
            with session_factory.begin() as session:
                settle_login_success(session, user_id, deps.charge.clock())
                needs_otp = totp_enabled(session, user_id)
        except AccountLockedError as lock:
            raise too_many_attempts(lock.retry_after) from None
        return user_id, needs_otp

    def too_many_attempts(retry_after: int) -> HTTPException:
        return HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts", headers={"Retry-After": str(retry_after)}
        )

    def second_factor(user_id: uuid.UUID, needs_otp: bool, totp_code: SecretStr | None) -> tuple[str, ...]:
        """amr after a password success; a user with TOTP enabled must also pass the code check.
        `check_password` reads `needs_otp` after argon2, so an enrolment confirmed meanwhile is not missed."""
        if not needs_otp:
            return (AMR_FIRST_FACTOR,)
        if deps.totp_encryption_key is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "mfa not configured")
        if totp_code is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mfa required")
        try:
            check_totp(
                session_factory,
                deps.charge.clock,
                deps.totp_encryption_key,
                user_id,
                totp_code.get_secret_value(),
            )
        except InvalidTotpCodeError:
            write_user_audit(user_id, "auth.login_failed", {"factor": "totp"})
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials") from None
        except TotpLockedError:
            raise HTTPException(status.HTTP_423_LOCKED, "mfa locked") from None
        except TotpDecryptError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "mfa unavailable") from None
        return (AMR_FIRST_FACTOR, AMR_OTP)

    def write_user_audit(user_id: uuid.UUID, action: str, summary: dict[str, object]) -> None:
        with session_factory.begin() as session:
            session.add(
                AuditLog(
                    actor_id=f"user:{user_id}",
                    actor_role="consumer",
                    action=action,
                    target=f"user:{user_id}",
                    summary=summary,
                    correlation_id=uuid.uuid4().hex,
                    occurred_at=deps.charge.clock(),
                )
            )

    def session_user(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> uuid.UUID:
        if credentials is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing session")
        if deps.jwt_private_key_pem is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session")
        try:
            claims = verify_session_token(
                deps.jwt_private_key_pem, credentials.credentials, deps.charge.clock()
            )
        except InvalidSessionError:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session") from None
        with session_factory() as session:
            needs_otp = totp_enabled(session, claims.user_id)
        if needs_otp and AMR_OTP not in claims.amr:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "mfa required")
        return claims.user_id

    def totp_key() -> str:
        if deps.totp_encryption_key is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "mfa not configured")
        return deps.totp_encryption_key

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
        user_id, needs_otp = check_password(body)
        second_factor(user_id, needs_otp, body.totp_code)
        token = issue_user_token(deps.jwt_private_key_pem, user_id, deps.charge.clock())
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return UserTokenResponse(access_token=token, expires_in=USER_TOKEN_TTL_SECONDS)

    @app.post("/v1/sessions", status_code=status.HTTP_201_CREATED)
    def create_session(body: SessionRequest, response: Response) -> SessionResponse:
        if deps.jwt_private_key_pem is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "token issuing not configured")
        user_id, needs_otp = check_password(body)
        amr = second_factor(user_id, needs_otp, body.totp_code)
        token = issue_session_token(deps.jwt_private_key_pem, user_id, deps.charge.clock(), amr)
        write_user_audit(user_id, "auth.login", {"amr": list(amr)})
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return SessionResponse(access_token=token, expires_in=SESSION_TOKEN_TTL_SECONDS, amr=list(amr))

    @app.post("/v1/me/mfa/totp", status_code=status.HTTP_201_CREATED)
    def enrol_mfa_totp(
        user_id: Annotated[uuid.UUID, Depends(session_user)],
        key: Annotated[str, Depends(totp_key)],
        response: Response,
    ) -> TotpEnrolResponse:
        try:
            enrolment = enrol_totp(session_factory, deps.charge.clock, key, user_id)
        except TotpAlreadyEnabledError:
            raise HTTPException(status.HTTP_409_CONFLICT, "totp already enabled") from None
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        return TotpEnrolResponse(secret=enrolment.secret, otpauth_uri=enrolment.otpauth_uri)

    @app.post("/v1/me/mfa/totp/verify")
    def verify_mfa_totp(
        body: TotpVerifyRequest,
        user_id: Annotated[uuid.UUID, Depends(session_user)],
        key: Annotated[str, Depends(totp_key)],
    ) -> TotpVerifyResponse:
        try:
            confirm_totp(session_factory, deps.charge.clock, key, user_id, body.code.get_secret_value())
        except InvalidTotpCodeError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid code") from None
        except TotpNotEnrolledError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "totp not enrolled") from None
        except TotpAlreadyEnabledError:
            raise HTTPException(status.HTTP_409_CONFLICT, "totp already enabled") from None
        except TotpLockedError:
            raise HTTPException(status.HTTP_423_LOCKED, "mfa locked") from None
        except TotpDecryptError:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "mfa unavailable") from None
        return TotpVerifyResponse(totp_enabled=True)

    @app.get("/v1/me/wallets")
    def my_wallets(
        user_id: Annotated[uuid.UUID, Depends(session_user)], response: Response
    ) -> WalletListResponse:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        with session_factory() as session:
            return WalletListResponse(items=list_wallets(session, user_id))

    @app.get("/v1/me/transfers")
    def my_transfers(
        user_id: Annotated[uuid.UUID, Depends(session_user)],
        response: Response,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> TransferHistoryResponse:
        try:
            with session_factory() as session:
                page = list_transfers(session, user_id, limit, cursor)
        except InvalidCursorError:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "invalid cursor", headers=NO_STORE_HEADERS
            ) from None
        response.headers.update(NO_STORE_HEADERS)
        return TransferHistoryResponse(items=page.items, next_cursor=page.next_cursor)

    @app.post("/v1/me/transfers")
    def create_transfer(
        body: TransferRequest,
        user_id: Annotated[uuid.UUID, Depends(session_user)],
        idempotency_key: Annotated[str | None, Header(max_length=255)] = None,
    ) -> JSONResponse:
        try:
            key = _require_idempotency_key(idempotency_key)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, exc.detail, headers=NO_STORE_HEADERS) from None
        outcome = transfer(
            session_factory,
            deps.charge.clock,
            user_id,
            key,
            body.recipient_email,
            body.amount_minor,
            body.currency,
        )
        return JSONResponse(outcome.body, status_code=outcome.status_code, headers=NO_STORE_HEADERS)

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

    @app.get("/v1/payments/{payment_id}/risk")
    def payment_risk(
        payment_id: uuid.UUID, merchant_id: Annotated[uuid.UUID, Depends(merchant_from_api_key)]
    ) -> PaymentRiskView:
        try:
            with session_factory() as session:
                return get_payment_risk(session, merchant_id, payment_id)
        except PaymentNotFoundError:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found") from None

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
    return JSONResponse(
        {"error": "validation_failed", "detail": detail}, status_code=422, headers=NO_STORE_HEADERS
    )
