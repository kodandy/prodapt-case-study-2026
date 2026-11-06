"""Tests for the SQ3 sink hardening (M1/M2/L1/L4), written from docs/specs/sq3.md N-4 "Hardening"."""

import logging
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from ops_ai.notify import (
    AlertNotificationMessage,
    AlertSinkError,
    WebhookAlertSink,
    load_alert_sink,
)

SECRET = "s" * 40
URL = "https://alerts.example.test/hooks/fraud"
URL_PATH_TOKEN = "SECRETTOKEN"
NOW = datetime(2026, 11, 6, 9, 30, 15, tzinfo=UTC)
NOTIFICATION_ID = uuid.UUID("dddddddd-0000-4000-8000-000000000005")
BODY = b'{"alert_id":"x","schema":"payplat.fraud_alert.v1"}'
MESSAGE = AlertNotificationMessage(notification_id=NOTIFICATION_ID, body=BODY)
DEAD_PROXY = "http://127.0.0.1:9"
NOISY_LOGGERS = ("httpx", "httpcore")


class _BodyReadError(Exception):
    """Raised if anything iterates the response body."""


class _ExplodingStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        raise _BodyReadError("the response body must not be read")


class _Receiver(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        server = self.server
        assert isinstance(server, _RecordingServer)
        server.received.append((self.path, self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class _RecordingServer(HTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Receiver)
        self.received: list[tuple[str, bytes]] = []


@pytest.fixture(autouse=True)
def _pristine_http_loggers() -> Iterator[None]:
    """The loader changes global logger levels; start each test from NOTSET and restore afterwards."""
    previous = {name: logging.getLogger(name).level for name in NOISY_LOGGERS}
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)
    try:
        yield
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)


@pytest.fixture
def receiver() -> Iterator[_RecordingServer]:
    server = _RecordingServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _loopback_url(server: _RecordingServer) -> str:
    return f"http://127.0.0.1:{server.server_port}/{URL_PATH_TOKEN}/hook"


def _environ(url: str) -> dict[str, str]:
    return {"OPS_AI_ALERT_WEBHOOK_URL": url, "OPS_AI_ALERT_WEBHOOK_SECRET": SECRET}


def _sink(handler: httpx.MockTransport) -> WebhookAlertSink:
    return WebhookAlertSink(URL, SECRET, httpx.Client(transport=handler, follow_redirects=False))


# --- M1: no log line holds the URL ------------------------------------------------------------------------


def test_a_delivered_post_leaves_no_log_record_holding_the_url_path_with_root_logging_at_info(
    receiver: _RecordingServer, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    caplog.set_level(logging.INFO)
    sink = load_alert_sink(_environ(_loopback_url(receiver)))

    # Act
    sink.send(MESSAGE, NOW)

    # Assert
    assert [path for path, _ in receiver.received] == [f"/{URL_PATH_TOKEN}/hook"]
    leaking = [record.getMessage() for record in caplog.records if URL_PATH_TOKEN in record.getMessage()]
    assert leaking == []


def test_loading_a_webhook_sink_raises_the_httpx_and_httpcore_loggers_to_warning() -> None:
    # Arrange
    environ = _environ(URL)

    # Act
    load_alert_sink(environ)

    # Assert
    assert [logging.getLogger(name).level for name in NOISY_LOGGERS] == [logging.WARNING, logging.WARNING]


# --- M2: the response body is never read ------------------------------------------------------------------


def test_a_2xx_response_whose_body_stream_raises_does_not_make_send_raise() -> None:
    # Arrange
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=_ExplodingStream())

    sink = _sink(httpx.MockTransport(handle))

    # Act
    sink.send(MESSAGE, NOW)

    # Assert
    assert [request.content for request in requests] == [MESSAGE.body]


def test_a_500_response_whose_body_stream_raises_still_raises_http_500() -> None:
    # Arrange
    sink = _sink(httpx.MockTransport(lambda request: httpx.Response(500, stream=_ExplodingStream())))

    # Act
    with pytest.raises(AlertSinkError) as raised:
        sink.send(MESSAGE, NOW)

    # Assert
    assert raised.value.reason == "http_500"


# --- L1: any client exception becomes an AlertSinkError ---------------------------------------------------


def test_a_non_httpx_exception_from_the_client_becomes_a_sink_error_naming_the_class() -> None:
    # Arrange
    def explode(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("secret-bearing detail https://alerts.example.test/hooks/fraud")

    sink = _sink(httpx.MockTransport(explode))

    # Act
    with pytest.raises(AlertSinkError) as raised:
        sink.send(MESSAGE, NOW)

    # Assert
    assert raised.value.reason == "RuntimeError"
    assert str(raised.value) == "RuntimeError"


# --- L4: the loaded client ignores proxy env vars ---------------------------------------------------------


def test_the_loaded_sink_delivers_to_a_loopback_webhook_despite_dead_proxy_env_vars(
    receiver: _RecordingServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, DEAD_PROXY)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    sink = load_alert_sink(_environ(_loopback_url(receiver)))

    # Act
    sink.send(MESSAGE, NOW)

    # Assert
    assert receiver.received == [(f"/{URL_PATH_TOKEN}/hook", BODY)]
