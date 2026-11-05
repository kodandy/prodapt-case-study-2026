"""Tests for ops_ai.observability.tracing (provider, trace id helpers, log filter), from the NFR spec C."""

import logging
from collections.abc import Iterator

import pytest
from opentelemetry.sdk.trace import TracerProvider
from ops_ai.observability.tracing import (
    LOG_FORMAT,
    TraceIdLogFilter,
    configure_logging,
    configure_tracing,
    current_trace_id,
    current_traceparent,
)

EXPECTED_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s trace_id=%(trace_id)s %(message)s"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _record() -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, __file__, 1, "message", None, None)


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    uvicorn_state = {
        name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate)
        for name in UVICORN_LOGGERS
    }
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    for name, (uvicorn_handlers, propagate) in uvicorn_state.items():
        logging.getLogger(name).handlers[:] = uvicorn_handlers
        logging.getLogger(name).propagate = propagate


def test_log_format_is_the_trace_correlated_format() -> None:
    # Arrange
    expected = EXPECTED_LOG_FORMAT

    # Act
    actual = LOG_FORMAT

    # Assert
    assert actual == expected


def test_configure_tracing_returns_the_same_sdk_provider_on_every_call() -> None:
    # Arrange
    first = configure_tracing("ops-ai")

    # Act
    second = configure_tracing("ops-ai")
    third = configure_tracing("another-name")

    # Assert
    assert isinstance(first, TracerProvider)
    assert second is first
    assert third is first


def test_trace_helpers_return_none_outside_any_span() -> None:
    # Arrange
    configure_tracing("ops-ai")

    # Act
    trace_id = current_trace_id()
    traceparent = current_traceparent()

    # Assert
    assert trace_id is None
    assert traceparent is None


def test_trace_helpers_describe_the_active_span() -> None:
    # Arrange
    tracer = configure_tracing("ops-ai").get_tracer("test")

    # Act
    with tracer.start_as_current_span("work") as span:
        trace_id = current_trace_id()
        traceparent = current_traceparent()
        expected_trace = format(span.get_span_context().trace_id, "032x")
        expected_span = format(span.get_span_context().span_id, "016x")

    # Assert
    assert trace_id == expected_trace
    assert traceparent == f"00-{expected_trace}-{expected_span}-01"


def test_trace_id_log_filter_stamps_a_dash_outside_a_span_and_keeps_the_record() -> None:
    # Arrange
    record = _record()

    # Act
    kept = TraceIdLogFilter().filter(record)

    # Assert
    assert kept is True
    assert record.trace_id == "-"  # type: ignore[attr-defined]


def test_trace_id_log_filter_stamps_the_active_trace_id() -> None:
    # Arrange
    tracer = configure_tracing("ops-ai").get_tracer("test")
    record = _record()

    # Act
    with tracer.start_as_current_span("work") as span:
        kept = TraceIdLogFilter().filter(record)
        expected = format(span.get_span_context().trace_id, "032x")

    # Assert
    assert kept is True
    assert record.trace_id == expected  # type: ignore[attr-defined]


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_installs_a_root_handler_with_the_format_and_the_filter() -> None:
    # Arrange
    root = logging.getLogger()

    # Act
    configure_logging(logging.WARNING)

    # Assert
    formatted = [h for h in root.handlers if h.formatter is not None and h.formatter._fmt == LOG_FORMAT]
    assert len(formatted) == 1
    assert [type(f) for f in formatted[0].filters] == [TraceIdLogFilter]
    assert root.level == logging.WARNING


@pytest.mark.usefixtures("restore_logging")
def test_configure_logging_makes_the_uvicorn_loggers_propagate_to_root() -> None:
    # Arrange
    for name in UVICORN_LOGGERS:
        logging.getLogger(name).propagate = False

    # Act
    configure_logging()

    # Assert
    assert [logging.getLogger(name).propagate for name in UVICORN_LOGGERS] == [True, True, True]
