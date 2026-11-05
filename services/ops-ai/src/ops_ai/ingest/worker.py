import logging
import signal
import socket
import threading
from datetime import datetime, timedelta
from types import FrameType

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock, system_clock
from ops_ai.compliance.consumer import consume_compliance
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.ingest.consumer import consume_payments
from ops_ai.tickets.sources import sweep_unknown_payments

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 0.2
REDIS_CONNECT_TIMEOUT_SECONDS = 2.0
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0
UNKNOWN_SWEEP_INTERVAL = timedelta(seconds=60)


def run_consumer(
    session_factory: sessionmaker[Session],
    redis: Redis,
    clock: Clock,
    consumer_name: str,
    stop: threading.Event,
    block_ms: int = 500,
) -> int:
    """Consumes gw.payments.v1 and gw.compliance.v1 until `stop` is set; returns the number of entries read
    (re-reads included).

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
        except (RedisError, SQLAlchemyError, OSError) as error:
            logger.warning("consumer iteration failed: %s", type(error).__name__)
            stop.wait(RETRY_BACKOFF_SECONDS)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_ops_ai_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=REDIS_CONNECT_TIMEOUT_SECONDS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    )
    stop = threading.Event()

    def _request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)
    consumer_name = f"ops-ai-{socket.gethostname()}"
    total = run_consumer(session_factory, redis, system_clock, consumer_name, stop)
    logger.info("consumer stopped after reading %d entries", total)


if __name__ == "__main__":
    main()
