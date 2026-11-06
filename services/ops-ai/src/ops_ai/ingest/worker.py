import logging
import os
import signal
import socket
import threading
from datetime import datetime, timedelta
from types import FrameType

from contracts.events import DISPUTES_TOPIC, PAYMENTS_TOPIC
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock, system_clock
from ops_ai.compliance.consumer import consume_compliance
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.ingest.consumer import PAYMENT_FACTS_GROUP, consume_payments
from ops_ai.ingest.disputes import DISPUTE_WORKFLOWS_GROUP, consume_disputes
from ops_ai.observability.consumer import record_consumer_lag
from ops_ai.observability.server import start_metrics_server
from ops_ai.observability.tracing import configure_logging
from ops_ai.tickets.sources import sweep_unknown_payments

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 0.2
REDIS_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0
UNKNOWN_SWEEP_INTERVAL = timedelta(seconds=60)
LAG_SAMPLE_SECONDS = 5.0


def run_consumer(
    session_factory: sessionmaker[Session],
    redis: Redis,
    clock: Clock,
    consumer_name: str,
    stop: threading.Event,
    block_ms: int = 500,
) -> int:
    """Consumes gw.payments.v1, gw.compliance.v1 and gw.disputes.v1 until `stop` is set; returns the number of
    entries read (re-reads included).

    Each iteration first re-reads this consumer's unacked entries, so an event whose apply failed is retried.
    Redis and DB errors are logged by class name only (messages can carry connection details) and retried.
    At most once per UNKNOWN_SWEEP_INTERVAL of `clock` it also tickets payments left `unknown` (B12.1).
    """
    total = 0
    last_sweep: datetime | None = None
    while not stop.is_set():
        now = clock()
        if last_sweep is None or now - last_sweep >= UNKNOWN_SWEEP_INTERVAL:
            # Own try: a failing sweep must not stop ingest (and the card-testing watch it feeds).
            try:
                with session_factory.begin() as session:
                    sweep_unknown_payments(session, clock)
            except (SQLAlchemyError, OSError) as error:
                logger.warning("unknown-payment sweep failed: %s", type(error).__name__)
            last_sweep = now
        try:
            total += consume_payments(session_factory, redis, clock, consumer_name, start_id="0")
            total += consume_payments(session_factory, redis, clock, consumer_name, block_ms=block_ms)
            total += consume_compliance(session_factory, redis, clock, consumer_name, start_id="0")
            total += consume_compliance(session_factory, redis, clock, consumer_name)
            total += consume_disputes(session_factory, redis, clock, consumer_name, start_id="0")
            total += consume_disputes(session_factory, redis, clock, consumer_name)
        except (RedisError, SQLAlchemyError, OSError) as error:
            logger.warning("consumer iteration failed: %s", type(error).__name__)
            stop.wait(RETRY_BACKOFF_SECONDS)
    return total


def sample_consumer_lag(
    redis: Redis, stop: threading.Event, interval_seconds: float = LAG_SAMPLE_SECONDS
) -> None:
    """Thread loop for the consumer-lag alert: XINFO GROUPS every `interval_seconds` until `stop`."""
    while not stop.is_set():
        try:
            record_consumer_lag(redis, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
            record_consumer_lag(redis, DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP)
        except (RedisError, OSError) as error:
            logger.warning("consumer lag sample failed: %s", type(error).__name__)
        stop.wait(interval_seconds)


def make_worker_redis(redis_url: str) -> Redis:
    """Decodes replies as UTF-8 with replacement: an entry that is not valid UTF-8 then reaches the consumer
    (and its dead-letter) instead of failing every XREADGROUP of its stream."""
    return Redis.from_url(
        redis_url,
        decode_responses=True,
        encoding_errors="replace",
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )


def main() -> None:
    configure_logging(logging.INFO)
    start_metrics_server(os.environ.get("METRICS_PORT"))
    settings = load_ops_ai_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    redis = make_worker_redis(settings.redis_url)
    stop = threading.Event()

    def _request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    threading.Thread(target=sample_consumer_lag, args=(redis, stop), name="consumer-lag", daemon=True).start()
    consumer_name = f"ops-ai-{socket.gethostname()}"
    total = run_consumer(session_factory, redis, system_clock, consumer_name, stop)
    logger.info("consumer stopped after reading %d entries", total)


if __name__ == "__main__":
    main()
