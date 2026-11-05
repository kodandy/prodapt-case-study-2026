"""W3C trace context across services: gateway request -> outbox -> stream -> ops-ai consumer -> LLM span."""

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.observability.tracing import configure_tracing as configure_gateway_tracing
from gateway.outbox.relay import publish_pending
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, MockLlm
from ops_ai.db import make_session_factory as make_ops_ai_session_factory
from ops_ai.ingest.consumer import consume_payments
from ops_ai.observability.llm import InstrumentedLlm
from ops_ai.observability.tracing import configure_tracing as configure_ops_ai_tracing
from redis import Redis
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

STREAM = "gw.payments.v1"
TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
PARENT_SPAN_ID = "b7ad6b7169203331"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"
TRACEPARENT_PATTERN = re.compile(r"00-([0-9a-f]{32})-[0-9a-f]{16}-01")
CARD = {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}
CONSUME_SPAN = "consume gw.payments.v1"


@dataclass(frozen=True)
class Stack:
    client: TestClient
    auth: dict[str, str]
    gateway_sessions: sessionmaker[Session]
    ops_ai_sessions: sessionmaker[Session]
    redis: Redis
    clock: FixedClock
    merchant_id: uuid.UUID


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    gateway_provider = configure_gateway_tracing("gateway")
    ops_ai_provider = configure_ops_ai_tracing("ops-ai")
    assert ops_ai_provider is gateway_provider
    memory = InMemorySpanExporter()
    ops_ai_provider.add_span_processor(SimpleSpanProcessor(memory))
    yield memory
    memory.clear()


@pytest.fixture
def stack(
    session_factory: sessionmaker[Session], engine: Engine, redis_client: Redis, clock: FixedClock
) -> Stack:
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=redis_client,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    client = TestClient(create_app(GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER)))
    return Stack(
        client=client,
        auth={"Authorization": f"Bearer {credentials.api_key}"},
        gateway_sessions=session_factory,
        ops_ai_sessions=make_ops_ai_session_factory(engine),
        redis=redis_client,
        clock=clock,
        merchant_id=credentials.merchant_id,
    )


def _charge(stack: Stack, traceparent: str | None) -> tuple[int, str]:
    headers = {**stack.auth, "Idempotency-Key": f"key-{uuid.uuid4()}"}
    if traceparent is not None:
        headers["traceparent"] = traceparent
    response = stack.client.post(
        "/v1/payments",
        json={"amount_minor": 2500, "currency": "USD", "method": "card", "card": CARD},
        headers=headers,
    )
    return response.status_code, response.headers["traceparent"]


def _trace_id_of(traceparent: str) -> str:
    match = TRACEPARENT_PATTERN.fullmatch(traceparent)
    assert match is not None, traceparent
    return match.group(1)


def _outbox_traceparents(stack: Stack) -> list[str | None]:
    with stack.gateway_sessions() as session:
        rows = session.execute(text("SELECT headers FROM gateway.outbox ORDER BY id")).scalars()
        return [headers.get("traceparent") for headers in rows]


def _spans_named(exporter: InMemorySpanExporter, name: str) -> list[str]:
    return [format(s.context.trace_id, "032x") for s in exporter.get_finished_spans() if s.name == name]


def test_trace_id_of_the_gateway_request_reaches_the_outbox_row_and_the_stream_entry(
    stack: Stack, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    traceparent_in = TRACEPARENT

    # Act
    status_code, echoed = _charge(stack, traceparent_in)
    stored = _outbox_traceparents(stack)
    published = publish_pending(stack.gateway_sessions, stack.redis, stack.clock)
    [(_entry_id, fields)] = stack.redis.xrange(STREAM)

    # Assert
    assert status_code == 201
    assert _trace_id_of(echoed) == TRACE_ID
    assert [_trace_id_of(str(value)) for value in stored] == [TRACE_ID]
    assert published == 1
    assert _trace_id_of(fields["traceparent"]) == TRACE_ID
    assert sorted(fields) == ["event_id", "payload", "traceparent"]
    assert _spans_named(exporter, "POST /v1/payments") == [TRACE_ID]


def test_trace_id_of_the_gateway_request_reaches_the_ops_ai_consumer_span(
    stack: Stack, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _charge(stack, TRACEPARENT)
    publish_pending(stack.gateway_sessions, stack.redis, stack.clock)

    # Act
    consumed = consume_payments(stack.ops_ai_sessions, stack.redis, stack.clock, "e2e-trace-consumer")

    # Assert
    assert consumed == 1
    assert _spans_named(exporter, CONSUME_SPAN) == [TRACE_ID]


def test_without_an_incoming_traceparent_the_gateway_trace_still_reaches_the_consumer(
    stack: Stack, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _, echoed = _charge(stack, None)
    gateway_trace = _trace_id_of(echoed)
    publish_pending(stack.gateway_sessions, stack.redis, stack.clock)

    # Act
    consume_payments(stack.ops_ai_sessions, stack.redis, stack.clock, "e2e-trace-consumer")

    # Assert
    assert gateway_trace != TRACE_ID
    assert [_trace_id_of(str(value)) for value in _outbox_traceparents(stack)] == [gateway_trace]
    assert _spans_named(exporter, CONSUME_SPAN) == [gateway_trace]


def test_the_llm_span_joins_the_trace_of_the_request_that_created_the_event(
    stack: Stack, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    _charge(stack, TRACEPARENT)
    publish_pending(stack.gateway_sessions, stack.redis, stack.clock)
    [(_entry_id, fields)] = stack.redis.xrange(STREAM)
    parent = TraceContextTextMapPropagator().extract({"traceparent": fields["traceparent"]})
    tracer = configure_ops_ai_tracing("ops-ai").get_tracer("e2e")
    llm = InstrumentedLlm(MockLlm())
    request = LlmRequest(
        question="declined payments",
        evidence=(EvidenceItem(ref="aggregate:decline_code:card_declined", summary="1 declined payment"),),
    )

    # Act
    with tracer.start_as_current_span("ops-ai.ask", context=parent):
        response = llm.complete(request)

    # Assert
    assert response.model == "mock"
    assert _spans_named(exporter, "llm.complete") == [TRACE_ID]
    assert _spans_named(exporter, "ops-ai.ask") == [TRACE_ID]
