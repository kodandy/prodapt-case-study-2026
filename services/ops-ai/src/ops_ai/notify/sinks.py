import hashlib
import hmac
import logging
from collections.abc import Mapping
from datetime import datetime

import httpx

from ops_ai.assistant.openai_llm import LOOPBACK_HOSTS
from ops_ai.notify.service import AlertNotificationMessage, AlertSink, AlertSinkError

WEBHOOK_URL_ENV = "OPS_AI_ALERT_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "OPS_AI_ALERT_WEBHOOK_SECRET"  # noqa: S105 - env var name, not a secret
MIN_SECRET_LENGTH = 32
WEBHOOK_TIMEOUT_SECONDS = 5.0
_URL_ERROR = (
    f"{WEBHOOK_URL_ENV} must be https (or http on a loopback host) with no userinfo, query or fragment"
)
_SECRET_ERROR = f"{WEBHOOK_SECRET_ENV} must be set with at least {MIN_SECRET_LENGTH} characters"

logger = logging.getLogger(__name__)


class LogAlertSink:
    """Default sink: one log line per notification, no network."""

    def send(self, message: AlertNotificationMessage, now: datetime) -> None:
        logger.info("alert notification sent notification_id=%s sink=log", message.notification_id)


class WebhookAlertSink:
    """POSTs the body signed with HMAC-SHA256 over `<timestamp>.<body>`."""

    def __init__(self, url: str, secret: str, client: httpx.Client) -> None:
        self._url = url
        self._secret = secret.encode()
        self._client = client

    def __repr__(self) -> str:
        return "WebhookAlertSink()"

    def send(self, message: AlertNotificationMessage, now: datetime) -> None:
        timestamp = str(int(now.timestamp()))
        digest = hmac.new(self._secret, timestamp.encode() + b"." + message.body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": str(message.notification_id),
            "X-Payplat-Timestamp": timestamp,
            "X-Payplat-Signature": f"v1={digest}",
        }
        try:
            response = self._client.post(self._url, content=message.body, headers=headers)
        except httpx.HTTPError as error:
            raise AlertSinkError(type(error).__name__) from None
        if not 200 <= response.status_code < 300:
            raise AlertSinkError(f"http_{response.status_code}")


def _valid_webhook_url(url: str) -> bool:
    try:
        parsed = httpx.URL(url)
    except Exception:
        return False
    if parsed.userinfo or parsed.query or parsed.fragment or not parsed.host:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.host in LOOPBACK_HOSTS)


def load_alert_sink(environ: Mapping[str, str]) -> AlertSink:
    """The webhook sink when the URL env var is set, else the log sink.

    Errors never echo the URL or the secret.
    """
    url = environ.get(WEBHOOK_URL_ENV, "")
    if not url:
        return LogAlertSink()
    if not _valid_webhook_url(url):
        raise ValueError(_URL_ERROR)
    secret = environ.get(WEBHOOK_SECRET_ENV, "")
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(_SECRET_ERROR)
    client = httpx.Client(timeout=WEBHOOK_TIMEOUT_SECONDS, follow_redirects=False)
    return WebhookAlertSink(url, secret, client)
