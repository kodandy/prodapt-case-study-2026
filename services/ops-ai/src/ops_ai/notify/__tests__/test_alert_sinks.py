"""Tests for the alert notification sinks and load_alert_sink, written from docs/specs/sq3.md N-4."""

import hashlib
import hmac
import json
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
    LogAlertSink,
    WebhookAlertSink,
    deliver_alert_notifications,
    enqueue_alert_notification,
    load_alert_sink,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SECRET = "s" * 40
URL = "https://alerts.example.test/hooks/fraud"
NOW = datetime(2026, 11, 6, 9, 30, 15, tzinfo=UTC)
NOTIFICATION_ID = uuid.UUID("dddddddd-0000-4000-8000-000000000004")
BODY = b'{"alert_id":"x","schema":"payplat.fraud_alert.v1"}'
MESSAGE = AlertNotificationMessage(notification_id=NOTIFICATION_ID, body=BODY)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


def _signature(secret: str, timestamp: str, body: bytes) -> str:
    return "v1=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _sink(handler: httpx.MockTransport, url: str = URL) -> WebhookAlertSink:
    return WebhookAlertSink(url, SECRET, httpx.Client(transport=handler, follow_redirects=False))


def _recording_transport(status: int, requests: list[httpx.Request]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status)

    return httpx.MockTransport(handle)


# --- LogAlertSink -----------------------------------------------------------------------------------------


def test_the_log_sink_logs_one_info_line_with_the_notification_id_and_not_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    sink = LogAlertSink()
    caplog.set_level(logging.INFO, logger="ops_ai.notify.sinks")

    # Act
    sink.send(MESSAGE, NOW)

    # Assert
    records = [r for r in caplog.records if r.name == "ops_ai.notify.sinks"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, f"alert notification sent notification_id={NOTIFICATION_ID} sink=log")
    ]


# --- WebhookAlertSink -------------------------------------------------------------------------------------


def test_the_webhook_posts_the_body_with_idempotency_key_timestamp_and_a_valid_signature() -> None:
    # Arrange
    requests: list[httpx.Request] = []
    sink = _sink(_recording_transport(204, requests))

    # Act
    sink.send(MESSAGE, NOW)

    # Assert
    (request,) = requests
    timestamp = str(int(NOW.timestamp()))
    assert (request.method, str(request.url), request.content) == ("POST", URL, BODY)
    assert request.headers["content-type"] == "application/json"
    assert request.headers["idempotency-key"] == str(NOTIFICATION_ID)
    assert request.headers["x-payplat-timestamp"] == timestamp
    assert request.headers["x-payplat-signature"] == _signature(SECRET, timestamp, BODY)


def test_the_signature_does_not_verify_with_another_secret() -> None:
    # Arrange
    requests: list[httpx.Request] = []
    sink = _sink(_recording_transport(200, requests))

    # Act
    sink.send(MESSAGE, NOW)

    # Assert
    (request,) = requests
    timestamp = request.headers["x-payplat-timestamp"]
    assert request.headers["x-payplat-signature"] != _signature("t" * 40, timestamp, BODY)
    assert SECRET not in " ".join(request.headers.values())


@pytest.mark.parametrize("status", [301, 400, 404, 500, 503])
def test_a_non_2xx_status_raises_a_sink_error_naming_the_status(status: int) -> None:
    # Arrange
    sink = _sink(_recording_transport(status, []))

    # Act
    with pytest.raises(AlertSinkError) as raised:
        sink.send(MESSAGE, NOW)

    # Assert
    assert raised.value.reason == f"http_{status}"


def test_a_transport_error_raises_a_sink_error_naming_the_error_class() -> None:
    # Arrange
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    sink = _sink(httpx.MockTransport(refuse))

    # Act
    with pytest.raises(AlertSinkError) as raised:
        sink.send(MESSAGE, NOW)

    # Assert
    assert raised.value.reason == "ConnectError"


def test_the_webhook_sink_repr_holds_neither_the_url_nor_the_secret() -> None:
    # Arrange
    sink = _sink(_recording_transport(200, []))

    # Act
    rendered = repr(sink)

    # Assert
    assert SECRET not in rendered
    assert "alerts.example.test" not in rendered


# --- load_alert_sink --------------------------------------------------------------------------------------


@pytest.mark.parametrize("environ", [{}, {"OPS_AI_ALERT_WEBHOOK_URL": ""}])
def test_without_a_webhook_url_the_sink_is_the_log_sink(environ: dict[str, str]) -> None:
    # Arrange / Act
    sink = load_alert_sink(environ)

    # Assert
    assert isinstance(sink, LogAlertSink)


@pytest.mark.parametrize(
    "url",
    [
        "https://alerts.example.test/hook",
        "http://127.0.0.1:9000/hook",
        "http://localhost:9000/hook",
        "http://[::1]:9000/hook",
    ],
)
def test_an_https_or_loopback_http_url_with_a_secret_gives_a_webhook_sink(url: str) -> None:
    # Arrange
    environ = {"OPS_AI_ALERT_WEBHOOK_URL": url, "OPS_AI_ALERT_WEBHOOK_SECRET": SECRET}

    # Act
    sink = load_alert_sink(environ)

    # Assert
    assert isinstance(sink, WebhookAlertSink)


@pytest.mark.parametrize(
    "url",
    [
        "http://alerts.example.test/hook",
        "http://10.0.0.5/hook",
        "ftp://alerts.example.test/hook",
        "https://user:pw-sentinel@alerts.example.test/hook",
        "https://alerts.example.test/hook?token=q-sentinel",
        "https://alerts.example.test/hook#frag-sentinel",
        "https:///no-host",
        "not a url",
    ],
)
def test_an_unsafe_url_is_refused_with_a_message_that_does_not_echo_it(url: str) -> None:
    # Arrange
    environ = {"OPS_AI_ALERT_WEBHOOK_URL": url, "OPS_AI_ALERT_WEBHOOK_SECRET": SECRET}

    # Act
    with pytest.raises(ValueError) as raised:
        load_alert_sink(environ)

    # Assert
    message = str(raised.value)
    assert url not in message
    assert "sentinel" not in message
    assert "alerts.example.test" not in message


@pytest.mark.parametrize("secret", [None, "", "short-secret-sentinel"])
def test_a_missing_or_short_secret_is_refused_without_echoing_it(secret: str | None) -> None:
    # Arrange
    environ = {"OPS_AI_ALERT_WEBHOOK_URL": URL}
    if secret is not None:
        environ["OPS_AI_ALERT_WEBHOOK_SECRET"] = secret

    # Act
    with pytest.raises(ValueError) as raised:
        load_alert_sink(environ)

    # Assert
    assert "sentinel" not in str(raised.value)
    assert URL not in str(raised.value)


# --- end to end over a real loopback HTTP server ----------------------------------------------------------


class _Receiver(BaseHTTPRequestHandler):
    received: list[tuple[dict[str, str], bytes]] = []

    def do_POST(self) -> None:
        length = int(self.headers["Content-Length"])
        type(self).received.append(({k.lower(): v for k, v in self.headers.items()}, self.rfile.read(length)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def receiver() -> Iterator[tuple[str, list[tuple[dict[str, str], bytes]]]]:
    _Receiver.received = []
    server = HTTPServer(("127.0.0.1", 0), _Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/hook", _Receiver.received
    finally:
        server.shutdown()
        server.server_close()


def test_a_pending_notification_reaches_a_loopback_webhook_signed_and_once(
    session_factory: sessionmaker[Session],
    receiver: tuple[str, list[tuple[dict[str, str], bytes]]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    url, received = receiver
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, "
                "started_at, last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :key, "
                ":merchant_id, 'card_testing', :first, :at, :at, 12, '{}'::jsonb, :at)"
            ),
            {"alert_id": alert_id, "key": f"k:{alert_id}", "merchant_id": MERCHANT, "first": uuid.uuid4(), "at": NOW},
        )
        notification_id = enqueue_alert_notification(
            session,
            lambda: NOW,
            fraud_alert_id=alert_id,
            merchant_id=MERCHANT,
            kind="card_testing",
            payment_count=12,
            features={"micro_payments": 12, "card_bins": ["424242"]},
        )
    sink = load_alert_sink({"OPS_AI_ALERT_WEBHOOK_URL": url, "OPS_AI_ALERT_WEBHOOK_SECRET": SECRET})
    caplog.set_level(logging.DEBUG)

    # Act
    first = deliver_alert_notifications(session_factory, sink, lambda: NOW)
    second = deliver_alert_notifications(session_factory, sink, lambda: NOW)

    # Assert
    assert (first, second) == (1, 0)
    ((headers, body),) = received
    payload = json.loads(body)
    assert payload["alert_id"] == str(alert_id)
    assert payload["merchant_id"] == str(MERCHANT)
    assert payload["payment_count"] == 12
    assert "424242" not in body.decode()
    assert headers["idempotency-key"] == str(notification_id)
    assert headers["x-payplat-signature"] == _signature(SECRET, headers["x-payplat-timestamp"], body)
    assert SECRET not in caplog.text
    assert headers["x-payplat-signature"] not in caplog.text
