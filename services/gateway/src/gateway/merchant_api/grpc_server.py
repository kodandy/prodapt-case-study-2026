"""Merchant gRPC API (B8.2): the same domain functions, idempotency store, validation and tenancy as REST.

`python -m gateway.merchant_api.grpc_server` serves it on GATEWAY_GRPC_PORT (default 50051).
"""

import functools
import logging
import os
import uuid
from collections.abc import Callable, Sequence
from concurrent import futures
from dataclasses import dataclass, field
from typing import NoReturn

import grpc
from google.protobuf import any_pb2
from google.rpc import code_pb2, error_details_pb2, status_pb2  # type: ignore[import-untyped]  # no py.typed
from grpc_status import rpc_status

from gateway.accounts.service import authenticate_api_key
from gateway.config import load_gateway_settings
from gateway.main import build_charge_deps
from gateway.merchant_api.pb import payments_pb2, payments_pb2_grpc
from gateway.merchant_api.validation import (
    FieldViolation,
    RequestValidationFailedError,
    require_idempotency_key,
    validate_charge_request,
    validate_payment_history_query,
)
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import PaymentNotFoundError
from gateway.payments.history import InvalidCursorError, get_payment, list_payments
from gateway.payments.schemas import ChargeResponse, PaymentSummary

MAX_MESSAGE_BYTES = 64 * 1024
MAX_CONCURRENT_RPCS = 64
DEFAULT_GRPC_PORT = 50051
IDEMPOTENCY_LABEL = "idempotency-key metadata"
_BEARER_PREFIX = "Bearer "

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrpcDeps:
    charge: ChargeDeps
    api_key_pepper: str = field(repr=False)


class _RpcAbortedError(Exception):
    """Raised after `context.abort*`: grpc already holds the status, so the guard must let it through."""


def _abort_invalid(context: grpc.ServicerContext, violations: Sequence[FieldViolation]) -> NoReturn:
    bad_request = error_details_pb2.BadRequest(
        field_violations=[
            error_details_pb2.BadRequest.FieldViolation(field=v.field, description=v.description)
            for v in violations
        ]
    )
    detail = any_pb2.Any()
    detail.Pack(bad_request)
    status = status_pb2.Status(code=code_pb2.INVALID_ARGUMENT, message="invalid request", details=[detail])
    try:
        context.abort_with_status(rpc_status.to_status(status))
    except Exception as exc:
        raise _RpcAbortedError from exc
    raise AssertionError("abort_with_status returned")


def _abort(context: grpc.ServicerContext, code: grpc.StatusCode, details: str) -> NoReturn:
    try:
        context.abort(code, details)
    except Exception as exc:
        raise _RpcAbortedError from exc
    raise AssertionError("abort returned")


def _metadata_value(context: grpc.ServicerContext, name: str) -> str | None:
    for key, value in context.invocation_metadata():
        if key == name and isinstance(value, str):
            return value
    return None


def _payment_message(summary: PaymentSummary) -> payments_pb2.Payment:
    data = summary.model_dump(mode="json")
    message = payments_pb2.Payment(
        payment_id=str(summary.payment_id),
        state=summary.state,
        amount_minor=summary.amount_minor,
        currency=summary.currency,
        method=summary.method,
        refunded_minor=summary.refunded_minor,
        disputed_minor=summary.disputed_minor,
        chargeback_minor=summary.chargeback_minor,
        created_at=str(data["created_at"]),
    )
    if summary.card is not None:
        message.card.CopyFrom(payments_pb2.CardSummary(bin=summary.card.bin, last4=summary.card.last4))
    if summary.decline_code is not None:
        message.decline_code = summary.decline_code
    return message


def _charge_message(response: ChargeResponse) -> payments_pb2.CreatePaymentResponse:
    message = payments_pb2.CreatePaymentResponse(
        payment_id=str(response.payment_id),
        state=response.state,
        amount_minor=response.amount_minor,
        currency=response.currency,
        method=response.method,
        card=payments_pb2.CardSummary(bin=response.card.bin, last4=response.card.last4),
    )
    if response.decline_code is not None:
        message.decline_code = response.decline_code
    return message


def _guarded[Servicer, Request, Response](
    rpc: Callable[[Servicer, Request, grpc.ServicerContext], Response],
) -> Callable[[Servicer, Request, grpc.ServicerContext], Response]:
    """grpc would send `str(exc)` of an unexpected error to the client (DB hosts, SQL parameters): answer a
    fixed INTERNAL instead and log only the exception type. Deliberate aborts pass through unchanged."""

    @functools.wraps(rpc)
    def wrapper(self: Servicer, request: Request, context: grpc.ServicerContext) -> Response:
        try:
            return rpc(self, request, context)
        except _RpcAbortedError:
            raise
        except Exception as exc:
            _logger.error("grpc %s failed: %s", rpc.__name__, type(exc).__name__)
            _abort(context, grpc.StatusCode.INTERNAL, "internal error")

    return wrapper


class MerchantPaymentsServicer(payments_pb2_grpc.MerchantPaymentsServicer):
    def __init__(self, deps: GrpcDeps) -> None:
        self._deps = deps

    def _merchant(self, context: grpc.ServicerContext) -> uuid.UUID:
        authorization = _metadata_value(context, "authorization")
        if authorization is None or not authorization.startswith(_BEARER_PREFIX):
            _abort(context, grpc.StatusCode.UNAUTHENTICATED, "missing api key")
        api_key = authorization.removeprefix(_BEARER_PREFIX).strip()
        if not api_key:
            _abort(context, grpc.StatusCode.UNAUTHENTICATED, "missing api key")
        with self._deps.charge.session_factory() as session:
            merchant_id = authenticate_api_key(session, api_key, self._deps.api_key_pepper)
        if merchant_id is None:
            _abort(context, grpc.StatusCode.UNAUTHENTICATED, "invalid api key")
        return merchant_id

    @_guarded
    def CreatePayment(
        self, request: payments_pb2.CreatePaymentRequest, context: grpc.ServicerContext
    ) -> payments_pb2.CreatePaymentResponse:
        merchant_id = self._merchant(context)
        payload: dict[str, object] = {"amount_minor": request.amount_minor, "currency": request.currency}
        if request.method:
            payload["method"] = request.method
        if request.HasField("card"):
            payload["card"] = {
                "number": request.card.number,
                "exp_month": request.card.exp_month,
                "exp_year": request.card.exp_year,
                "cvc": request.card.cvc,
            }
        try:
            charge_request = validate_charge_request(payload)
            key = require_idempotency_key(_metadata_value(context, "idempotency-key"), IDEMPOTENCY_LABEL)
        except RequestValidationFailedError as exc:
            _abort_invalid(context, exc.violations)
        outcome = charge_card(self._deps.charge, merchant_id, key, charge_request)
        if outcome.status_code == 422:
            _abort_invalid(context, [FieldViolation("idempotency_key", "idempotency_key_reused")])
        if outcome.status_code == 409:
            _abort(context, grpc.StatusCode.ABORTED, "idempotency_in_progress")
        if outcome.status_code not in (201, 202):
            _abort(context, grpc.StatusCode.INTERNAL, "internal error")
        return _charge_message(ChargeResponse.model_validate(outcome.body))

    @_guarded
    def GetPayment(
        self, request: payments_pb2.GetPaymentRequest, context: grpc.ServicerContext
    ) -> payments_pb2.Payment:
        merchant_id = self._merchant(context)
        try:
            payment_id = uuid.UUID(request.payment_id)
        except ValueError:
            _abort_invalid(context, [FieldViolation("payment_id", "invalid payment_id")])
        try:
            with self._deps.charge.session_factory() as session:
                summary = get_payment(session, merchant_id, payment_id)
        except PaymentNotFoundError:
            _abort(context, grpc.StatusCode.NOT_FOUND, "payment not found")
        return _payment_message(summary)

    @_guarded
    def ListPayments(
        self, request: payments_pb2.ListPaymentsRequest, context: grpc.ServicerContext
    ) -> payments_pb2.ListPaymentsResponse:
        merchant_id = self._merchant(context)
        try:
            query = validate_payment_history_query(
                limit=None if request.limit == 0 else request.limit,
                cursor=request.cursor if request.HasField("cursor") else None,
                state=request.state if request.HasField("state") else None,
                created_from=request.created_from if request.HasField("created_from") else None,
                created_to=request.created_to if request.HasField("created_to") else None,
                method=request.method if request.HasField("method") else None,
            )
        except RequestValidationFailedError as exc:
            _abort_invalid(context, exc.violations)
        try:
            with self._deps.charge.session_factory() as session:
                page = list_payments(
                    session,
                    merchant_id,
                    query.limit,
                    query.cursor,
                    query.state,
                    query.created_from,
                    query.created_to,
                    query.method,
                )
        except InvalidCursorError:
            _abort_invalid(context, [FieldViolation("cursor", "invalid cursor")])
        response = payments_pb2.ListPaymentsResponse(items=[_payment_message(item) for item in page.items])
        if page.next_cursor is not None:
            response.next_cursor = page.next_cursor
        return response


def build_grpc_server(
    deps: GrpcDeps, address: str = "[::]:50051", max_workers: int = 16
) -> tuple[grpc.Server, int]:
    """Returns the server (not started) and the bound port; port 0 in `address` picks a free one."""
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=max_workers),
        maximum_concurrent_rpcs=MAX_CONCURRENT_RPCS,
        options=[
            ("grpc.max_receive_message_length", MAX_MESSAGE_BYTES),
            ("grpc.max_send_message_length", MAX_MESSAGE_BYTES * 16),
            ("grpc.max_concurrent_streams", 32),
            ("grpc.max_connection_idle_ms", 60_000),
            ("grpc.max_connection_age_ms", 300_000),
        ],
    )
    payments_pb2_grpc.add_MerchantPaymentsServicer_to_server(MerchantPaymentsServicer(deps), server)
    port = server.add_insecure_port(address)
    return server, port


def main() -> None:
    settings = load_gateway_settings()
    deps = GrpcDeps(
        charge=build_charge_deps(settings), api_key_pepper=settings.api_key_pepper.get_secret_value()
    )
    port = int(os.environ.get("GATEWAY_GRPC_PORT", str(DEFAULT_GRPC_PORT)))
    server, _bound = build_grpc_server(deps, f"[::]:{port}")
    server.start()
    server.wait_for_termination()


if __name__ == "__main__":
    main()
