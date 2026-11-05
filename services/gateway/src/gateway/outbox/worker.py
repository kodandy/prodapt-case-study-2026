import logging
import os
import signal
import threading

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock, system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from gateway.observability.metrics import OUTBOX_PUBLISHED
from gateway.observability.outbox import record_outbox_backlog
from gateway.observability.server import start_metrics_server
from gateway.observability.tracing import configure_logging
from gateway.outbox.relay import publish_pending

logger = logging.getLogger("gateway.outbox.worker")
BACKLOG_SAMPLE_SECONDS = 5.0


def run_relay(
    session_factory: sessionmaker[Session],
    redis: Redis,
    clock: Clock,
    stop: threading.Event,
    poll_interval_seconds: float = 0.2,
) -> int:
    """Calls publish_pending until `stop` is set; returns the total published.

    A Redis or DB error is logged by class name only (messages can carry connection details), then retried
    after the poll interval. The loop never exits on such an error.
    """
    total = 0
    while not stop.is_set():
        try:
            published = publish_pending(session_factory, redis, clock)
        except (RedisError, SQLAlchemyError, OSError) as error:
            logger.warning("relay iteration failed: %s", type(error).__name__)
            stop.wait(poll_interval_seconds)
            continue
        total += published
        OUTBOX_PUBLISHED.inc(published)
        if published == 0:
            stop.wait(poll_interval_seconds)
    return total


def sample_backlog(
    session_factory: sessionmaker[Session], clock: Clock, stop: threading.Event, interval_seconds: float
) -> None:
    """Refreshes the outbox backlog gauges until `stop` is set (own thread; the relay loop is unchanged)."""
    while not stop.is_set():
        try:
            record_outbox_backlog(session_factory, clock)
        except (SQLAlchemyError, OSError) as error:
            logger.warning("backlog sample failed: %s", type(error).__name__)
        stop.wait(interval_seconds)


def main() -> None:
    configure_logging(logging.INFO)
    start_metrics_server(os.environ.get("METRICS_PORT"))
    settings = load_gateway_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=2.0, socket_timeout=5.0)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    threading.Thread(
        target=sample_backlog,
        args=(session_factory, system_clock, stop, BACKLOG_SAMPLE_SECONDS),
        name="outbox-backlog",
        daemon=True,
    ).start()
    logger.info("relay started")
    total = run_relay(session_factory, redis, system_clock, stop)
    logger.info("relay stopped after publishing %d events", total)


if __name__ == "__main__":
    main()
