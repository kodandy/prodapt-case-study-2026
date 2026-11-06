import argparse
import logging
import os
import signal
import threading

from sqlalchemy.exc import SQLAlchemyError

from ops_ai.clock import system_clock
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.notify import deliver_alert_notifications, load_alert_sink

logger = logging.getLogger("ops_ai.notify.worker")


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver pending fraud alert notifications")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    sink = load_alert_sink(os.environ)
    settings = load_ops_ai_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stop.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    logger.info("alert notifier started sink=%s", type(sink).__name__)
    try:
        while not stop.is_set():
            try:
                delivered = deliver_alert_notifications(session_factory, sink, system_clock)
            except (SQLAlchemyError, OSError) as error:
                # Class name only: messages can carry connection details.
                logger.warning("alert notifier iteration failed: %s", type(error).__name__)
                delivered = 0
            if args.once:
                return
            if delivered == 0:
                stop.wait(1.0)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
