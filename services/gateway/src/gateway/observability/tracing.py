import logging
import threading

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s"
TRACEPARENT = "traceparent"
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")
_lock = threading.Lock()
PROPAGATOR = TraceContextTextMapPropagator()


class Level1IdGenerator(RandomIdGenerator):
    """Random ids, but without the W3C level-2 `random-trace-id` flag, so `traceparent` keeps flags `01`
    (sampled) and stays readable by level-1 parsers in the stream consumers and proxies."""

    def is_trace_id_random(self) -> bool:
        return False


def configure_tracing(service_name: str) -> TracerProvider:
    """The process-wide SDK provider, installed as the global one on the first call (later calls, from either
    service in a shared test process, return the same provider). No exporter is attached here: spans feed
    the trace ids in logs and stream headers; an OTLP exporter is a deployment choice
    (docs/production-readiness.md)."""
    with _lock:
        current = trace.get_tracer_provider()
        if isinstance(current, TracerProvider):
            return current
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name}), id_generator=Level1IdGenerator()
        )
        trace.set_tracer_provider(provider)
        return provider


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.is_valid else None


def current_traceparent() -> str | None:
    if not trace.get_current_span().get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    PROPAGATOR.inject(carrier)
    return carrier.get(TRACEPARENT)


class TraceIdLogFilter(logging.Filter):
    """Stamps `trace_id` on every record ("-" outside a span) so LOG_FORMAT can print it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = current_trace_id() or "-"
        return True


def configure_logging(level: int = logging.INFO) -> None:
    """One root handler with LOG_FORMAT; uvicorn's loggers propagate to it, so access lines carry trace_id."""
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)
    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers:
        handler.addFilter(TraceIdLogFilter())
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
