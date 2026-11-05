"""Tests that the HTTP method label is bounded: standard methods keep their name, any other token is OTHER.

The registry is process-global, so every assertion compares a before/after delta.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from ops_ai.observability.http import install_observability, method_label
from ops_ai.observability.metrics import REGISTRY
from starlette.types import Scope

REQUESTS_METRIC = "ops_ai_http_requests_total"
LATENCY_COUNT_METRIC = "ops_ai_http_request_duration_seconds_count"
UNMATCHED_PATH = "/v1/does-not-exist/abc123"
NON_STANDARD_METHODS = ["PROPFIND", "XYZZY123"]
STANDARD_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


def _requests(method: str, route: str, status: str) -> float:
    value = REGISTRY.get_sample_value(REQUESTS_METRIC, {"method": method, "route": route, "status": status})
    return 0.0 if value is None else value


def _latency_count(method: str, route: str) -> float:
    value = REGISTRY.get_sample_value(LATENCY_COUNT_METRIC, {"method": method, "route": route})
    return 0.0 if value is None else value


def _recorded_label_values(label: str) -> set[str]:
    return {
        sample.labels[label]
        for family in REGISTRY.collect()
        if family.name.startswith("ops_ai_http_request")
        for sample in family.samples
        if label in sample.labels
    }


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    install_observability(app)
    return TestClient(app)


@pytest.mark.parametrize("method", NON_STANDARD_METHODS)
def test_non_standard_method_increments_the_request_counter_under_other(
    client: TestClient, method: str
) -> None:
    # Arrange
    before = _requests("OTHER", "unmatched", "404")

    # Act
    response = client.request(method, UNMATCHED_PATH)

    # Assert
    assert response.status_code == 404
    assert _requests("OTHER", "unmatched", "404") - before == 1.0
    assert _requests(method, "unmatched", "404") == 0.0


@pytest.mark.parametrize("method", NON_STANDARD_METHODS)
def test_non_standard_method_increments_the_latency_histogram_count_under_other(
    client: TestClient, method: str
) -> None:
    # Arrange
    before = _latency_count("OTHER", "unmatched")

    # Act
    client.request(method, UNMATCHED_PATH)

    # Assert
    assert _latency_count("OTHER", "unmatched") - before == 1.0
    assert _latency_count(method, "unmatched") == 0.0


@pytest.mark.parametrize("method", NON_STANDARD_METHODS)
def test_non_standard_method_token_never_appears_as_a_label_value_in_any_sample(
    client: TestClient, method: str
) -> None:
    # Arrange
    path = f"{UNMATCHED_PATH}/{method.lower()}"

    # Act
    client.request(method, path)

    # Assert
    assert method not in _recorded_label_values("method")
    assert path not in _recorded_label_values("route")
    assert method not in client.get("/metrics").text


def test_custom_method_to_an_unknown_path_is_labelled_with_the_unmatched_route(client: TestClient) -> None:
    # Arrange
    before = _requests("OTHER", "unmatched", "404")

    # Act
    client.request("PROPFIND", UNMATCHED_PATH)

    # Assert
    assert _requests("OTHER", "unmatched", "404") - before == 1.0
    assert _requests("OTHER", UNMATCHED_PATH, "404") == 0.0
    assert _latency_count("OTHER", UNMATCHED_PATH) == 0.0


def test_standard_get_keeps_its_own_method_label(client: TestClient) -> None:
    # Arrange
    before_get = _requests("GET", "/ping", "200")
    before_other = _requests("OTHER", "/ping", "200")
    before_latency = _latency_count("GET", "/ping")

    # Act
    response = client.get("/ping")

    # Assert
    assert response.status_code == 200
    assert _requests("GET", "/ping", "200") - before_get == 1.0
    assert _requests("OTHER", "/ping", "200") - before_other == 0.0
    assert _latency_count("GET", "/ping") - before_latency == 1.0


@pytest.mark.parametrize("method", STANDARD_METHODS)
def test_method_label_returns_a_standard_method_unchanged(method: str) -> None:
    # Arrange
    scope: Scope = {"type": "http", "method": method}

    # Act
    label = method_label(scope)

    # Assert
    assert label == method


@pytest.mark.parametrize("method", [*NON_STANDARD_METHODS, "get", ""])
def test_method_label_maps_a_non_standard_method_to_other(method: str) -> None:
    # Arrange
    scope: Scope = {"type": "http", "method": method}

    # Act
    label = method_label(scope)

    # Assert
    assert label == "OTHER"
