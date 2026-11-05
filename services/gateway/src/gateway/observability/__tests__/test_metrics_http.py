"""Tests for gateway.observability.http metrics: GET /metrics and the request counter/histogram middleware.

The registry is process-global, so every assertion compares a before/after delta.
"""

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.observability.http import install_observability
from gateway.observability.metrics import REGISTRY
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

REQUESTS_METRIC = "gateway_http_requests_total"
LATENCY_COUNT_METRIC = "gateway_http_request_duration_seconds_count"


def _raw_requests(method: str, route: str, status: str) -> float | None:
    return REGISTRY.get_sample_value(REQUESTS_METRIC, {"method": method, "route": route, "status": status})


def _requests(method: str, route: str, status: str) -> float:
    return _raw_requests(method, route, status) or 0.0


def _raw_latency_count(method: str, route: str) -> float | None:
    return REGISTRY.get_sample_value(LATENCY_COUNT_METRIC, {"method": method, "route": route})


def _latency_count(method: str, route: str) -> float:
    return _raw_latency_count(method, route) or 0.0


def _client(session_factory: sessionmaker[Session], clock: FixedClock) -> TestClient:
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return TestClient(create_app(GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _merchant_auth(session_factory: sessionmaker[Session], clock: FixedClock) -> dict[str, str]:
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    return {"Authorization": f"Bearer {credentials.api_key}"}


def test_metrics_endpoint_returns_prometheus_text_exposition(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    client.get("/healthz")

    # Act
    response = client.get("/metrics")

    # Assert
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE gateway_http_requests_total counter" in response.text
    assert "# TYPE gateway_http_request_duration_seconds histogram" in response.text
    assert 'gateway_http_requests_total{method="GET",route="/healthz",status="200"}' in response.text


def test_metrics_endpoint_is_added_by_install_observability_on_a_bare_app() -> None:
    # Arrange
    app = FastAPI()
    install_observability(app)
    client = TestClient(app)

    # Act
    response = client.get("/metrics")

    # Assert
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# TYPE gateway_http_requests_total counter" in response.text


def test_request_increments_the_counter_for_method_route_and_status(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    before = _requests("GET", "/healthz", "200")

    # Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200
    assert _requests("GET", "/healthz", "200") - before == 1.0


def test_two_requests_increment_the_counter_by_two(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    before = _requests("GET", "/healthz", "200")

    # Act
    client.get("/healthz")
    client.get("/healthz")

    # Assert
    assert _requests("GET", "/healthz", "200") - before == 2.0


def test_request_observes_the_latency_histogram_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    before = _latency_count("GET", "/healthz")

    # Act
    client.get("/healthz")

    # Assert
    assert _latency_count("GET", "/healthz") - before == 1.0


def test_route_label_is_the_template_not_the_concrete_path(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    headers = _merchant_auth(session_factory, clock)
    payment_id = uuid.uuid4()
    template = "/v1/payments/{payment_id}"
    before = _requests("GET", template, "404")

    # Act
    response = client.get(f"/v1/payments/{payment_id}", headers=headers)

    # Assert
    assert response.status_code == 404
    assert _requests("GET", template, "404") - before == 1.0
    assert _raw_requests("GET", f"/v1/payments/{payment_id}", "404") is None


def test_unmatched_path_is_counted_under_the_unmatched_route_label(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    path = f"/no-such-path/{uuid.uuid4()}"
    before = _requests("GET", "unmatched", "404")

    # Act
    response = client.get(path)

    # Assert
    assert response.status_code == 404
    assert _requests("GET", "unmatched", "404") - before == 1.0
    assert _raw_requests("GET", path, "404") is None
    assert _raw_latency_count("GET", path) is None


def test_error_status_is_recorded_in_the_status_label(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    before = _requests("GET", "/v1/payments", "401")

    # Act
    response = client.get("/v1/payments")

    # Assert
    assert response.status_code == 401
    assert _requests("GET", "/v1/payments", "401") - before == 1.0


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_method_label_distinguishes_http_methods(
    session_factory: sessionmaker[Session], clock: FixedClock, method: str
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    other = "POST" if method == "GET" else "GET"
    before = _requests(method, "/v1/payments", "401")
    before_other = _requests(other, "/v1/payments", "401")

    # Act
    response = client.request(method, "/v1/payments")

    # Assert
    assert response.status_code == 401
    assert _requests(method, "/v1/payments", "401") - before == 1.0
    assert _requests(other, "/v1/payments", "401") - before_other == 0.0


def test_scraping_metrics_does_not_count_the_metrics_request_itself(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    before = _requests("GET", "/metrics", "200")
    before_latency = _latency_count("GET", "/metrics")

    # Act
    client.get("/metrics")
    client.get("/metrics")

    # Assert
    assert _requests("GET", "/metrics", "200") - before == 0.0
    assert _latency_count("GET", "/metrics") - before_latency == 0.0


def test_metrics_endpoint_does_not_expose_the_merchant_api_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    headers = _merchant_auth(session_factory, clock)
    client.get("/v1/payments", headers=headers)
    api_key = headers["Authorization"].removeprefix("Bearer ")

    # Act
    body = client.get("/metrics").text

    # Assert
    assert api_key not in body
