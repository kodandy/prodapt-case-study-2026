"""Tests for ops_ai.observability.http (/metrics, request metrics and server spans), from the NFR spec C."""

import re
from collections.abc import Iterator

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from ops_ai.observability.http import install_observability
from ops_ai.observability.metrics import REGISTRY
from ops_ai.observability.tracing import configure_tracing, current_trace_id

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
PARENT_SPAN_ID = "00f067aa0ba902b7"
TRACEPARENT = f"00-{TRACE_ID}-{PARENT_SPAN_ID}-01"
TRACEPARENT_PATTERN = re.compile(r"00-([0-9a-f]{32})-([0-9a-f]{16})-01")
ITEM_ROUTE = "/items/{item_id}"
UNMATCHED_PATH = "/definitely-not-a-route-9c1e"


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _requests(method: str, route: str, status: str) -> float:
    return _sample("ops_ai_http_requests_total", {"method": method, "route": route, "status": status})


def _latency_count(method: str, route: str) -> float:
    return _sample("ops_ai_http_request_duration_seconds_count", {"method": method, "route": route})


def _trace_id_of(traceparent: str) -> str:
    match = TRACEPARENT_PATTERN.fullmatch(traceparent)
    assert match is not None, traceparent
    return match.group(1)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    configure_tracing("ops-ai").add_span_processor(SimpleSpanProcessor(memory))
    yield memory
    memory.clear()


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/items/{item_id}")
    def get_item(item_id: int) -> dict[str, str | int | None]:
        return {"item_id": item_id, "trace_id": current_trace_id()}

    @app.post("/conflict")
    def conflict() -> None:
        raise HTTPException(status_code=409, detail="conflict")

    install_observability(app)
    return TestClient(app)


def test_metrics_endpoint_returns_the_prometheus_text_exposition_of_the_ops_ai_registry(
    client: TestClient,
) -> None:
    # Arrange
    client.get("/items/1")

    # Act
    response = client.get("/metrics")

    # Assert
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE ops_ai_http_requests_total counter" in response.text
    assert "# TYPE ops_ai_http_request_duration_seconds histogram" in response.text


def test_each_request_increments_the_request_counter_under_the_route_template(client: TestClient) -> None:
    # Arrange
    before = _requests("GET", ITEM_ROUTE, "200")

    # Act
    first = client.get("/items/1")
    second = client.get("/items/2")

    # Assert
    assert (first.status_code, second.status_code) == (200, 200)
    assert _requests("GET", ITEM_ROUTE, "200") - before == 2.0
    assert _requests("GET", "/items/1", "200") == 0.0
    assert _requests("GET", "/items/2", "200") == 0.0


def test_each_request_is_observed_in_the_latency_histogram(client: TestClient) -> None:
    # Arrange
    before = _latency_count("GET", ITEM_ROUTE)

    # Act
    client.get("/items/7")

    # Assert
    assert _latency_count("GET", ITEM_ROUTE) - before == 1.0


def test_a_path_that_matches_no_route_is_counted_under_the_unmatched_label(client: TestClient) -> None:
    # Arrange
    before = _requests("GET", "unmatched", "404")

    # Act
    response = client.get(UNMATCHED_PATH)

    # Assert
    assert response.status_code == 404
    assert _requests("GET", "unmatched", "404") - before == 1.0
    assert _requests("GET", UNMATCHED_PATH, "404") == 0.0


def test_an_error_status_is_recorded_with_its_method_and_status_labels(client: TestClient) -> None:
    # Arrange
    before = _requests("POST", "/conflict", "409")

    # Act
    response = client.post("/conflict")

    # Assert
    assert response.status_code == 409
    assert _requests("POST", "/conflict", "409") - before == 1.0
    assert _requests("POST", "/conflict", "200") == 0.0


def test_scraping_metrics_is_not_counted_as_a_request(client: TestClient) -> None:
    # Arrange
    before = _requests("GET", "/metrics", "200")
    unmatched_before = _requests("GET", "unmatched", "200")

    # Act
    client.get("/metrics")
    client.get("/metrics")

    # Assert
    assert _requests("GET", "/metrics", "200") - before == 0.0
    assert _requests("GET", "unmatched", "200") - unmatched_before == 0.0


def test_a_valid_traceparent_is_joined_and_echoed_with_the_same_trace_id(
    client: TestClient, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    headers = {"traceparent": TRACEPARENT}

    # Act
    response = client.get("/items/3", headers=headers)

    # Assert
    assert response.status_code == 200
    assert _trace_id_of(response.headers["traceparent"]) == TRACE_ID
    assert response.json()["trace_id"] == TRACE_ID
    spans = [s for s in exporter.get_finished_spans() if format(s.context.trace_id, "032x") == TRACE_ID]
    assert [s.name for s in spans] == [f"GET {ITEM_ROUTE}"]


def test_the_server_span_is_a_child_of_the_incoming_parent_span(
    client: TestClient, exporter: InMemorySpanExporter
) -> None:
    # Arrange
    headers = {"traceparent": TRACEPARENT}

    # Act
    client.get("/items/3", headers=headers)

    # Assert
    [span] = [s for s in exporter.get_finished_spans() if format(s.context.trace_id, "032x") == TRACE_ID]
    assert span.kind == SpanKind.SERVER
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == PARENT_SPAN_ID


def test_a_request_without_traceparent_starts_a_new_trace_visible_inside_the_handler(
    client: TestClient,
) -> None:
    # Arrange
    headers: dict[str, str] = {}

    # Act
    response = client.get("/items/4", headers=headers)

    # Assert
    echoed = _trace_id_of(response.headers["traceparent"])
    assert echoed != TRACE_ID
    assert response.json()["trace_id"] == echoed


def test_a_malformed_traceparent_is_ignored_and_a_fresh_trace_is_started(client: TestClient) -> None:
    # Arrange
    headers = {"traceparent": "00-not-a-valid-trace-id-01"}

    # Act
    response = client.get("/items/5", headers=headers)

    # Assert
    assert response.status_code == 200
    echoed = _trace_id_of(response.headers["traceparent"])
    assert response.json()["trace_id"] == echoed
