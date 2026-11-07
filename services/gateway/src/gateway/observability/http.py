import os
import time

from fastapi import FastAPI, Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest
from prometheus_client.multiprocess import MultiProcessCollector
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS, REGISTRY
from gateway.observability.outbox import register_outbox_hooks
from gateway.observability.tracing import PROPAGATOR, TRACEPARENT, configure_tracing, current_traceparent

METRICS_PATH = "/metrics"
UNMATCHED_ROUTE = "unmatched"
KNOWN_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
OTHER_METHOD = "OTHER"
MULTIPROC_DIR_ENV = "PROMETHEUS_MULTIPROC_DIR"


def render_metrics() -> bytes:
    """Prometheus text for this service. With `PROMETHEUS_MULTIPROC_DIR` set (several uvicorn workers), the
    sum over every worker's files in that directory; otherwise this process's REGISTRY."""
    if not os.environ.get(MULTIPROC_DIR_ENV):
        return generate_latest(REGISTRY)
    registry = CollectorRegistry()
    MultiProcessCollector(registry)  # type: ignore[no-untyped-call]  # prometheus_client leaves it unannotated
    return generate_latest(registry)


def route_template(scope: Scope) -> str:
    """The matched route's path template; never the raw path, so 404 probes cannot grow label cardinality."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


def method_label(scope: Scope) -> str:
    """The request method if standard, else OTHER: clients choose the method token, so it is never a label
    as-is (arbitrary tokens would grow label cardinality without bound)."""
    method = scope["method"]
    return method if method in KNOWN_METHODS else OTHER_METHOD


class ObservabilityMiddleware:
    """Pure ASGI: one SERVER span per request (joining a valid incoming `traceparent`), the span's
    `traceparent` echoed on the response, and the request counter and latency histogram by route template."""

    def __init__(self, app: ASGIApp, service_name: str) -> None:
        self.app = app
        self.tracer = configure_tracing(service_name).get_tracer(service_name)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == METRICS_PATH:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        parent = PROPAGATOR.extract({TRACEPARENT: headers[TRACEPARENT]} if TRACEPARENT in headers else {})
        method = method_label(scope)
        status = 500
        started = time.perf_counter()
        with self.tracer.start_as_current_span(method, context=parent, kind=trace.SpanKind.SERVER) as span:
            traceparent = current_traceparent() or ""

            async def send_with_trace(message: Message) -> None:
                nonlocal status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    message["headers"] = [
                        *message.get("headers", []),
                        (TRACEPARENT.encode(), traceparent.encode()),
                    ]
                await send(message)

            try:
                await self.app(scope, receive, send_with_trace)
            finally:
                route = route_template(scope)
                span.update_name(f"{method} {route}")
                span.set_attribute("http.route", route)
                span.set_attribute("http.response.status_code", status)
                HTTP_REQUESTS.labels(method, route, str(status)).inc()
                HTTP_LATENCY.labels(method, route).observe(time.perf_counter() - started)


def disable_native_tracing(app: FastAPI) -> None:
    """FastAPI's built-in telemetry opens its own SERVER span once an SDK provider is global, which would give
    every request two server spans and no `traceparent` echo. ObservabilityMiddleware is the single owner."""
    native = getattr(app, "_telemetry", None)
    if isinstance(native, dict):
        native["tracing"] = False


def install_observability(app: FastAPI, service_name: str = "gateway") -> None:
    """GET /metrics (Prometheus text), request tracing and metrics, and the outbox trace/metric hook.
    Called last in `create_app` so the middleware wraps every other one (429s are counted too)."""
    register_outbox_hooks()
    disable_native_tracing(app)

    @app.get(METRICS_PATH, include_in_schema=False)
    def metrics() -> Response:
        return Response(render_metrics(), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(ObservabilityMiddleware, service_name=service_name)
