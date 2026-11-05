import time

from fastapi import FastAPI, Response
from opentelemetry import trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ops_ai.observability.metrics import HTTP_LATENCY, HTTP_REQUESTS, REGISTRY
from ops_ai.observability.tracing import PROPAGATOR, TRACEPARENT, configure_tracing, current_traceparent

METRICS_PATH = "/metrics"
UNMATCHED_ROUTE = "unmatched"


def route_template(scope: Scope) -> str:
    """The matched route's path template; never the raw path, so 404 probes cannot grow label cardinality."""
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


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
        method = scope["method"]
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


def install_observability(app: FastAPI, service_name: str = "ops-ai") -> None:
    """GET /metrics (Prometheus text) plus request tracing and metrics. Called last in `create_app` so the
    middleware wraps every other one."""
    disable_native_tracing(app)

    @app.get(METRICS_PATH, include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    app.add_middleware(ObservabilityMiddleware, service_name=service_name)
