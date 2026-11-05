"""B3.3 dispute HTTP routes: consumer and simulated-network opening, lists, network / ops resolution.

The ops route takes an ops session allowed `dispute.resolve` (RBAC) or, without a Bearer credential, the
interim `X-Ops-Admin-Token`. The network routes stand in for a card network's API: they carry no user role
(`public` in the RBAC table) and are guarded by `X-Network-Token` alone.
"""

import hmac
import uuid
from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from sqlalchemy.orm import Session, sessionmaker

from gateway.accounts.rbac import is_allowed
from gateway.admin import OpsGuard
from gateway.clock import Clock
from gateway.payments.dispute_lists import list_merchant_disputes, list_user_disputes
from gateway.payments.disputes import (
    DisputeAlreadyOpenError,
    DisputeAmountExceedsError,
    DisputeNotFoundError,
    InvalidDisputeTransitionError,
    PaymentNotDisputableError,
    PaymentNotFoundError,
    open_dispute,
    resolve_dispute,
    valid_dispute_reason,
)
from gateway.payments.history import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, InvalidCursorError
from gateway.payments.schemas import MAX_AMOUNT_MINOR, DisputeState, DisputeView

NETWORK_ACTOR = ("network", "card_network")
OPS_ACTOR = ("ops:admin", "ops_lead")
CONSUMER_ROLE = "consumer"
OPS_RESOLVE_ACTION = "dispute.resolve"


class _DisputeOpenFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(max_length=256)
    amount_minor: StrictInt | None = Field(default=None, gt=0, le=MAX_AMOUNT_MINOR)
    """Omitted → the payment's whole refundable amount."""

    @field_validator("reason")
    @classmethod
    def _reason_rules(cls, value: str) -> str:
        if not valid_dispute_reason(value):
            raise ValueError(
                "reason must be 1-64 characters, not blank, without control or format characters"
            )
        return value


class ConsumerDisputeRequest(_DisputeOpenFields):
    pass


class NetworkDisputeRequest(_DisputeOpenFields):
    payment_id: uuid.UUID


class ResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["won", "lost"]


class DisputeListResponse(BaseModel):
    items: list[DisputeView]
    next_cursor: str | None


def dispute_router(
    session_factory: sessionmaker[Session],
    clock: Clock,
    merchant_auth: Callable[[str], Callable[..., uuid.UUID]],
    user_auth: Callable[[str], Callable[..., uuid.UUID]],
    ops_guard: OpsGuard,
    network_token: str | None,
    ops_admin_token: str | None,
) -> APIRouter:
    """Routes:
    - POST /v1/me/payments/{payment_id}/disputes (Bearer session) {reason, amount_minor?} → 201 DisputeView;
      not the payer or unknown → 404; not succeeded / already active / amount above refundable → 409.
    - GET /v1/me/disputes?limit&cursor (Bearer session) → {items, next_cursor}: disputes on the user's
      payments, newest first.
    - GET /v1/disputes?limit&cursor&state (Bearer API key) → {items, next_cursor}: the merchant's disputes.
    - POST /v1/network/disputes (X-Network-Token) {payment_id, reason, amount_minor?} → 201 DisputeView.
    - POST /v1/network/disputes/{id}/resolution (X-Network-Token) {outcome} → 200 DisputeView.
    - POST /v1/ops/disputes/{id}/resolution (Bearer ops session with `dispute.resolve`, else the interim
      X-Ops-Admin-Token) {outcome} → 200 DisputeView, audited as the ops user. An invalid ops session → 401
      "invalid ops session"; an ops role outside the policy → 403 "forbidden".
      Resolution: unknown → 404; not `under_review` → 409. A missing or wrong token → 401; a token that is
      not configured → 503 for every request to those routes.
    """
    router = APIRouter()
    bearer = HTTPBearer(auto_error=False)

    def network_caller(x_network_token: Annotated[str | None, Header(max_length=512)] = None) -> None:
        _check_token(network_token, x_network_token, "network")

    def ops_caller(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
        x_ops_admin_token: Annotated[str | None, Header(max_length=512)] = None,
    ) -> tuple[str, str]:
        """Any Authorization header (whatever its scheme) must carry an ops session allowed `dispute.resolve`,
        with no fallback to the interim token; only a request without one is checked against the interim
        X-Ops-Admin-Token."""
        if request.headers.get("authorization") is not None:
            principal = ops_guard.authenticate(None if credentials is None else credentials.credentials)
            if not is_allowed(principal.role, OPS_RESOLVE_ACTION):
                raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
            request.state.ops_principal = principal
            return f"ops:{principal.ops_user_id}", principal.role
        _check_token(ops_admin_token, x_ops_admin_token, "ops")
        return OPS_ACTOR

    @router.post("/v1/me/payments/{payment_id}/disputes", status_code=status.HTTP_201_CREATED)
    def consumer_open_dispute(
        payment_id: uuid.UUID,
        body: ConsumerDisputeRequest,
        user_id: Annotated[uuid.UUID, Depends(user_auth("me.transfer.create"))],
    ) -> DisputeView:
        return _opened(
            lambda: open_dispute(
                session_factory,
                clock,
                payment_id,
                body.reason,
                body.amount_minor,
                payer_user_id=user_id,
                actor_id=f"user:{user_id}",
                actor_role=CONSUMER_ROLE,
            )
        )

    @router.get("/v1/me/disputes")
    def my_disputes(
        user_id: Annotated[uuid.UUID, Depends(user_auth("me.transfer.read"))],
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ) -> DisputeListResponse:
        try:
            with session_factory() as session:
                page = list_user_disputes(session, user_id, limit, cursor)
        except InvalidCursorError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
        return DisputeListResponse(items=page.items, next_cursor=page.next_cursor)

    @router.get("/v1/disputes")
    def merchant_disputes(
        merchant_id: Annotated[uuid.UUID, Depends(merchant_auth("merchant.dispute.read"))],
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        state: DisputeState | None = None,
    ) -> DisputeListResponse:
        try:
            with session_factory() as session:
                page = list_merchant_disputes(session, merchant_id, limit, cursor, state)
        except InvalidCursorError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid cursor") from None
        return DisputeListResponse(items=page.items, next_cursor=page.next_cursor)

    @router.post(
        "/v1/network/disputes", status_code=status.HTTP_201_CREATED, dependencies=[Depends(network_caller)]
    )
    def network_open_dispute(body: NetworkDisputeRequest) -> DisputeView:
        actor_id, actor_role = NETWORK_ACTOR
        return _opened(
            lambda: open_dispute(
                session_factory,
                clock,
                body.payment_id,
                body.reason,
                body.amount_minor,
                actor_id=actor_id,
                actor_role=actor_role,
            )
        )

    @router.post("/v1/network/disputes/{dispute_id}/resolution", dependencies=[Depends(network_caller)])
    def network_resolution(dispute_id: uuid.UUID, body: ResolutionRequest) -> DisputeView:
        return _resolved(session_factory, clock, dispute_id, body, NETWORK_ACTOR)

    @router.post("/v1/ops/disputes/{dispute_id}/resolution")
    def ops_resolution(
        dispute_id: uuid.UUID,
        body: ResolutionRequest,
        actor: Annotated[tuple[str, str], Depends(ops_caller)],
    ) -> DisputeView:
        return _resolved(session_factory, clock, dispute_id, body, actor)

    return router


def _check_token(expected: str | None, presented: str | None, name: Literal["network", "ops"]) -> None:
    if not expected:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"{name} not configured")
    if presented is None or not hmac.compare_digest(presented.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid {name} token")


def _opened(open_: Callable[[], DisputeView]) -> DisputeView:
    try:
        return open_()
    except PaymentNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "payment not found") from None
    except PaymentNotDisputableError:
        raise HTTPException(status.HTTP_409_CONFLICT, "payment not disputable") from None
    except DisputeAlreadyOpenError:
        raise HTTPException(status.HTTP_409_CONFLICT, "dispute already open") from None
    except DisputeAmountExceedsError:
        raise HTTPException(status.HTTP_409_CONFLICT, "dispute amount exceeds") from None


def _resolved(
    session_factory: sessionmaker[Session],
    clock: Clock,
    dispute_id: uuid.UUID,
    body: ResolutionRequest,
    actor: tuple[str, str],
) -> DisputeView:
    actor_id, actor_role = actor
    try:
        return resolve_dispute(session_factory, clock, dispute_id, body.outcome, actor_id, actor_role)
    except DisputeNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "dispute not found") from None
    except InvalidDisputeTransitionError:
        raise HTTPException(status.HTTP_409_CONFLICT, "invalid dispute transition") from None
