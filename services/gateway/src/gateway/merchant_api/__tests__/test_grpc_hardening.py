"""RED tests for gRPC hardening: concurrency cap, and no internal detail leaking out of unexpected errors."""

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import grpc
import pytest
from gateway.accounts.service import create_merchant
from gateway.clock import FixedClock
from gateway.merchant_api import grpc_server
from gateway.merchant_api.grpc_server import GrpcDeps, build_grpc_server
from gateway.merchant_api.pb import payments_pb2, payments_pb2_grpc
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.port import CardRef, ProviderResult
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY, TEST_NOW

EXPECTED_MAX_CONCURRENT_RPCS = 64
RPC_TIMEOUT_SECONDS = 20.0
SENSITIVE_TEXT = "db at 10.0.0.5:5432 secret-detail"
SENSITIVE_FRAGMENTS = ("10.0.0.5", "5432", "secret-detail", "RuntimeError", "Traceback")
INTERNAL_ERROR = "internal error"
MISSING_API_KEY = "missing api key"
INVALID_API_KEY = "invalid api key"
PAYMENT_NOT_FOUND = "payment not found"
APPROVE_PAN = "4242424242424242"

ServeFn = Callable[[GrpcDeps], payments_pb2_grpc.MerchantPaymentsStub]


class ExplodingSessionFactory(sessionmaker[Session]):
    """A session factory whose every use fails the way a dead database would."""

    def __call__(self, **local_kw: object) -> Session:
        raise RuntimeError(SENSITIVE_TEXT)


class ExplodingProvider:
    """A provider whose charge fails with a non-timeout exception carrying sensitive text."""

    name = "exploding"

    def charge(self, provider_key: str, amount_minor: int, currency: str, card: CardRef) -> ProviderResult:
        raise RuntimeError(SENSITIVE_TEXT)

    def lookup(self, provider_key: str) -> ProviderResult | None:
        return None


@dataclass(frozen=True)
class Failure:
    code: grpc.StatusCode
    details: str
    trailers: str


def _charge_deps(session_factory: sessionmaker[Session], clock: FixedClock) -> ChargeDeps:
    return ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=ExplodingProvider(),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )


@pytest.fixture
def serve() -> Iterator[ServeFn]:
    servers: list[grpc.Server] = []
    channels: list[grpc.Channel] = []

    def start(deps: GrpcDeps) -> payments_pb2_grpc.MerchantPaymentsStub:
        server, port = build_grpc_server(deps, "127.0.0.1:0")
        server.start()
        servers.append(server)
        channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        channels.append(channel)
        return payments_pb2_grpc.MerchantPaymentsStub(channel)

    try:
        yield start
    finally:
        for channel in channels:
            channel.close()
        for server in servers:
            server.stop(None)


def _failure_of(info: pytest.ExceptionInfo[grpc.RpcError]) -> Failure:
    error = info.value
    assert isinstance(error, grpc.Call)
    trailers = " ".join(f"{key}={value!r}" for key, value in error.trailing_metadata() or ())
    return Failure(code=error.code(), details=error.details() or "", trailers=trailers)


def _meta(api_key: str, idempotency_key: str | None = None) -> tuple[tuple[str, str], ...]:
    pairs = [("authorization", f"Bearer {api_key}")]
    if idempotency_key is not None:
        pairs.append(("idempotency-key", idempotency_key))
    return tuple(pairs)


def _create_request() -> payments_pb2.CreatePaymentRequest:
    request = payments_pb2.CreatePaymentRequest(amount_minor=1500, currency="USD", method="card")
    request.card.CopyFrom(payments_pb2.CardInput(number=APPROVE_PAN, exp_month=12, exp_year=2030, cvc="123"))
    return request


def _assert_no_sensitive_text(failure: Failure) -> None:
    for fragment in SENSITIVE_FRAGMENTS:
        assert fragment not in failure.details
        assert fragment not in failure.trailers


# --- concurrency cap -----------------------------------------------------------------------------------


def test_max_concurrent_rpcs_is_64() -> None:
    # Arrange / Act
    limit = grpc_server.MAX_CONCURRENT_RPCS

    # Assert
    assert limit == EXPECTED_MAX_CONCURRENT_RPCS


def test_build_grpc_server_still_binds_a_port_with_the_concurrency_cap(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps = GrpcDeps(charge=_charge_deps(session_factory, clock), api_key_pepper=TEST_API_KEY_PEPPER)

    # Act
    server, port = build_grpc_server(deps, "127.0.0.1:0")
    server.stop(None)

    # Assert
    assert port > 0


# --- unexpected exceptions are masked ------------------------------------------------------------------


@pytest.mark.parametrize("rpc_name", ["CreatePayment", "GetPayment", "ListPayments"])
def test_api_key_lookup_failure_returns_generic_internal_error(serve: ServeFn, rpc_name: str) -> None:
    # Arrange
    deps = GrpcDeps(
        charge=_charge_deps(ExplodingSessionFactory(), FixedClock(TEST_NOW)),
        api_key_pepper=TEST_API_KEY_PEPPER,
    )
    stub = serve(deps)
    calls: dict[str, Callable[[], object]] = {
        "CreatePayment": lambda: stub.CreatePayment(
            _create_request(),
            metadata=_meta("any-api-key", uuid.uuid4().hex),
            timeout=RPC_TIMEOUT_SECONDS,
        ),
        "GetPayment": lambda: stub.GetPayment(
            payments_pb2.GetPaymentRequest(payment_id=str(uuid.uuid4())),
            metadata=_meta("any-api-key"),
            timeout=RPC_TIMEOUT_SECONDS,
        ),
        "ListPayments": lambda: stub.ListPayments(
            payments_pb2.ListPaymentsRequest(), metadata=_meta("any-api-key"), timeout=RPC_TIMEOUT_SECONDS
        ),
    }

    # Act
    with pytest.raises(grpc.RpcError) as info:
        calls[rpc_name]()

    # Assert
    failure = _failure_of(info)
    assert (failure.code, failure.details) == (grpc.StatusCode.INTERNAL, INTERNAL_ERROR)
    _assert_no_sensitive_text(failure)


def test_provider_charge_failure_returns_generic_internal_error(
    serve: ServeFn, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        merchant = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    stub = serve(GrpcDeps(charge=_charge_deps(session_factory, clock), api_key_pepper=TEST_API_KEY_PEPPER))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.CreatePayment(
            _create_request(),
            metadata=_meta(merchant.api_key, uuid.uuid4().hex),
            timeout=RPC_TIMEOUT_SECONDS,
        )

    # Assert
    failure = _failure_of(info)
    assert (failure.code, failure.details) == (grpc.StatusCode.INTERNAL, INTERNAL_ERROR)
    _assert_no_sensitive_text(failure)


# --- intentional aborts are not swallowed --------------------------------------------------------------


def test_missing_api_key_is_still_unauthenticated(
    serve: ServeFn, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    stub = serve(GrpcDeps(charge=_charge_deps(session_factory, clock), api_key_pepper=TEST_API_KEY_PEPPER))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.ListPayments(payments_pb2.ListPaymentsRequest(), timeout=RPC_TIMEOUT_SECONDS)

    # Assert
    failure = _failure_of(info)
    assert (failure.code, failure.details) == (grpc.StatusCode.UNAUTHENTICATED, MISSING_API_KEY)


def test_unknown_api_key_is_still_unauthenticated(
    serve: ServeFn, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    stub = serve(GrpcDeps(charge=_charge_deps(session_factory, clock), api_key_pepper=TEST_API_KEY_PEPPER))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.ListPayments(
            payments_pb2.ListPaymentsRequest(), metadata=_meta("not-a-real-key"), timeout=RPC_TIMEOUT_SECONDS
        )

    # Assert
    failure = _failure_of(info)
    assert (failure.code, failure.details) == (grpc.StatusCode.UNAUTHENTICATED, INVALID_API_KEY)


def test_unknown_payment_is_still_not_found(
    serve: ServeFn, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        merchant = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    stub = serve(GrpcDeps(charge=_charge_deps(session_factory, clock), api_key_pepper=TEST_API_KEY_PEPPER))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.GetPayment(
            payments_pb2.GetPaymentRequest(payment_id=str(uuid.uuid4())),
            metadata=_meta(merchant.api_key),
            timeout=RPC_TIMEOUT_SECONDS,
        )

    # Assert
    failure = _failure_of(info)
    assert (failure.code, failure.details) == (grpc.StatusCode.NOT_FOUND, PAYMENT_NOT_FOUND)


def test_invalid_request_is_still_invalid_argument(
    serve: ServeFn, session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    with session_factory() as session:
        merchant = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    stub = serve(GrpcDeps(charge=_charge_deps(session_factory, clock), api_key_pepper=TEST_API_KEY_PEPPER))

    # Act
    with pytest.raises(grpc.RpcError) as info:
        stub.GetPayment(
            payments_pb2.GetPaymentRequest(payment_id="not-a-uuid"),
            metadata=_meta(merchant.api_key),
            timeout=RPC_TIMEOUT_SECONDS,
        )

    # Assert
    failure = _failure_of(info)
    assert (failure.code, failure.details) == (grpc.StatusCode.INVALID_ARGUMENT, "invalid request")
