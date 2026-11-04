import logging
import signal
import threading

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from gateway.clock import Clock, system_clock
from gateway.config import load_gateway_settings
from gateway.db import make_engine, make_session_factory
from gateway.notifications.service import process_pending_notifications

logger = logging.getLogger("gateway.notifications.worker")


def run_notifier(
    session_factory: sessionmaker[Session],
    clock: Clock,
    stop: threading.Event,
    poll_interval_seconds: float = 0.2,
) -> int:
    """Calls process_pending_notifications until `stop` is set; returns the total created.

    A DB error is logged by class name only (messages can carry connection details), then retried after
    the poll interval. The loop never exits on such an error.
    """
    total = 0
    while not stop.is_set():
        try:
            created = process_pending_notifications(session_factory, clock)
        except (SQLAlchemyError, OSError) as error:
            logger.warning("notifier iteration failed: %s", type(error).__name__)
            stop.wait(poll_interval_seconds)
            continue
        total += created
        if created == 0:
            stop.wait(poll_interval_seconds)
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_gateway_settings()
    session_factory = make_session_factory(make_engine(settings.database_url))
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    logger.info("notifier started")
    total = run_notifier(session_factory, system_clock, stop)
    logger.info("notifier stopped after creating %d notifications", total)


if __name__ == "__main__":
    main()
