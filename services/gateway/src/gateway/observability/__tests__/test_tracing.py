"""Tests for gateway.observability.tracing and the tracing half of gateway.observability.http."""

import logging
import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway.observability.http import install_observability
from gateway.observability.tracing import (
    LOG_FORMAT,
    TraceIdLogFilter,
    configure_logging,
    configure_tracing,
    current_trace_id,
    current_traceparent,
)
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

INCOMING_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING_SPAN_ID = "00f067aa0ba902b7"
INCOMING_TRACEPARENT = f"00-{INCOMING_TRACE_ID}-{INCOMING_SPAN_ID}-01"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
TRACEPARENT_PATTERN = re.compile(r"^00-(?P<trace_id>[0-9a-f]{32})-(?P<span_id>[0-9a-f]{16})-01$")


@pytest.fixture
def span_exporter() -> Iterator[InMemorySpanExporter]:
    provider = configure_tracing("gateway")
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    yield exporter
    processor.shutdown()


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    propagation = {name: logging.getLogger(name).propagate for name in _UVICORN_LOGGERS}
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, value in propagation.items():
        logging.getLogger(name).propagate = value


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    def probe() -> dict[str, str | None]:
        return {"trace_id": current_trace_id(), "traceparent": current_traceparent()}

    install_observability(app)
    return app


def _trace_id_of(traceparent: str) -> str:
    match = TRACEPARENT_PATTERN.match(traceparent)
    assert match is not None, traceparent
    return match.group("trace_id")


def test_configure_tracing_returns_an_sdk_tracer_provider() -> None:
    # Arrange / Act
    provider = configure_tracing("gateway")

    # Assert
    assert isinstance(provider, TracerProvider)


def test_configure_tracing_is_idempotent_and_returns_the_same_provider() -> None:
    # Arrange
    first = configure_tracing("gateway")

    # Act
    second = configure_tracing("gateway")
    third = configure_tracing("another-service")

    # Assert
    assert second is first
    assert third is first


def test_configure_tracing_installs_the_returned_provider_as_the_global_one() -> None:
    # Arrange / Act
    provider = configure_tracing("gateway")

    # Assert
    assert trace.get_tracer_provider() is provider


def test_current_trace_id_is_none_without_an_active_span() -> None:
    # Arrange
    configure_tracing("gateway")

    # Act
    trace_id = current_trace_id()

    # Assert
    assert trace_id is None


def test_current_traceparent_is_none_without_an_active_span() -> None:
    # Arrange
    configure_tracing("gateway")

    # Act
    traceparent = current_traceparent()

    # Assert
    assert traceparent is None


def test_current_trace_id_inside_a_span_is_the_span_trace_id_as_32_lowercase_hex() -> None:
    # Arrange
    tracer = configure_tracing("gateway").get_tracer("test")

    # Act
    with tracer.start_as_current_span("work") as span:
        trace_id = current_trace_id()

    # Assert
    assert trace_id == format(span.get_span_context().trace_id, "032x")
    assert re.fullmatch(r"[0-9a-f]{32}", trace_id or "") is not None


def test_current_traceparent_inside_a_span_is_the_w3c_header_of_that_span() -> None:
    # Arrange
    tracer = configure_tracing("gateway").get_tracer("test")

    # Act
    with tracer.start_as_current_span("work") as span:
        traceparent = current_traceparent()

    # Assert
    context = span.get_span_context()
    assert traceparent == f"00-{context.trace_id:032x}-{context.span_id:016x}-01"


def test_current_trace_id_returns_to_none_after_the_span_ends() -> None:
    # Arrange
    tracer = configure_tracing("gateway").get_tracer("test")
    with tracer.start_as_current_span("work"):
        pass

    # Act
    trace_id = current_trace_id()

    # Assert
    assert trace_id is None


def test_trace_id_log_filter_sets_a_dash_outside_a_span_and_keeps_the_record() -> None:
    # Arrange
    configure_tracing("gateway")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)

    # Act
    kept = TraceIdLogFilter().filter(record)

    # Assert
    assert kept is True
    assert record.trace_id == "-"  # type: ignore[attr-defined]


def test_trace_id_log_filter_sets_the_active_trace_id_inside_a_span() -> None:
    # Arrange
    tracer = configure_tracing("gateway").get_tracer("test")
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "msg", None, None)

    # Act
    with tracer.start_as_current_span("work") as span:
        kept = TraceIdLogFilter().filter(record)

    # Assert
    assert kept is True
    assert record.trace_id == format(span.get_span_context().trace_id, "032x")  # type: ignore[attr-defined]


def test_log_format_carries_the_trace_id_field() -> None:
    # Arrange / Act / Assert
    assert LOG_FORMAT == "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s"


def test_configure_logging_prints_the_trace_id_of_the_active_span(
    restore_root_logging: None, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange
    tracer = configure_tracing("gateway").get_tracer("test")
    configure_logging(logging.INFO)
    logger = logging.getLogger("gateway.test.logging")

    # Act
    with tracer.start_as_current_span("work") as span:
        logger.info("inside span")
    logger.info("outside span")

    # Assert
    captured = capsys.readouterr()
    lines = (captured.out + captured.err).splitlines()
    trace_id = format(span.get_span_context().trace_id, "032x")
    inside = f" INFO gateway.test.logging trace_id={trace_id} inside span"
    outside = " INFO gateway.test.logging trace_id=- outside span"
    assert any(line.endswith(inside) for line in lines)
    assert any(line.endswith(outside) for line in lines)


def test_configure_logging_sets_the_root_level(restore_root_logging: None) -> None:
    # Arrange / Act
    configure_logging(logging.WARNING)

    # Assert
    assert logging.getLogger().level == logging.WARNING


def test_configure_logging_makes_uvicorn_loggers_propagate_to_root(restore_root_logging: None) -> None:
    # Arrange
    for name in _UVICORN_LOGGERS:
        logging.getLogger(name).propagate = False

    # Act
    configure_logging(logging.INFO)

    # Assert
    assert [logging.getLogger(name).propagate for name in _UVICORN_LOGGERS] == [True, True, True]


def test_middleware_makes_the_request_trace_active_inside_the_handler_and_in_the_response_header(
    span_exporter: InMemorySpanExporter,
) -> None:
    # Arrange
    client = TestClient(_probe_app())

    # Act
    response = client.get("/probe")

    # Assert
    header_trace_id = _trace_id_of(response.headers["traceparent"])
    assert response.status_code == 200
    assert response.json()["trace_id"] == header_trace_id
    assert response.json()["traceparent"] is not None
    assert _trace_id_of(response.json()["traceparent"]) == header_trace_id


def test_middleware_joins_the_trace_of_an_incoming_traceparent(span_exporter: InMemorySpanExporter) -> None:
    # Arrange
    client = TestClient(_probe_app())

    # Act
    response = client.get("/probe", headers={"traceparent": INCOMING_TRACEPARENT})

    # Assert
    assert _trace_id_of(response.headers["traceparent"]) == INCOMING_TRACE_ID
    assert response.json()["trace_id"] == INCOMING_TRACE_ID
    server_spans = [s for s in span_exporter.get_finished_spans() if s.name == "GET /probe"]
    assert len(server_spans) == 1
    assert format(server_spans[0].context.trace_id, "032x") == INCOMING_TRACE_ID
    assert server_spans[0].parent is not None
    assert format(server_spans[0].parent.span_id, "016x") == INCOMING_SPAN_ID


def test_middleware_starts_a_server_span_named_method_and_route_template(
    span_exporter: InMemorySpanExporter,
) -> None:
    # Arrange
    app = FastAPI()

    @app.get("/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    install_observability(app)
    client = TestClient(app)

    # Act
    client.get("/items/abc-123")

    # Assert
    spans = [s for s in span_exporter.get_finished_spans() if s.name == "GET /items/{item_id}"]
    assert len(spans) == 1
    assert spans[0].kind == trace.SpanKind.SERVER


def test_middleware_names_the_span_unmatched_for_an_unknown_path(span_exporter: InMemorySpanExporter) -> None:
    # Arrange
    client = TestClient(_probe_app())

    # Act
    response = client.get("/definitely/not/here")

    # Assert
    assert response.status_code == 404
    assert [s.name for s in span_exporter.get_finished_spans()] == ["GET unmatched"]


@pytest.mark.parametrize(
    "bad_header", ["garbage", "00-short-bad-01", ""], ids=["garbage", "malformed", "empty"]
)
def test_middleware_starts_a_new_trace_when_the_traceparent_is_invalid(
    span_exporter: InMemorySpanExporter, bad_header: str
) -> None:
    # Arrange
    client = TestClient(_probe_app())

    # Act
    response = client.get("/probe", headers={"traceparent": bad_header})

    # Assert
    trace_id = _trace_id_of(response.headers["traceparent"])
    assert response.status_code == 200
    assert trace_id != INCOMING_TRACE_ID
    assert trace_id != "0" * 32
    assert response.json()["trace_id"] == trace_id


def test_two_requests_without_a_traceparent_get_different_traces(span_exporter: InMemorySpanExporter) -> None:
    # Arrange
    client = TestClient(_probe_app())

    # Act
    first = client.get("/probe")
    second = client.get("/probe")

    # Assert
    assert _trace_id_of(first.headers["traceparent"]) != _trace_id_of(second.headers["traceparent"])
