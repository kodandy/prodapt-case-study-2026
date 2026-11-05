import argparse
import time

from redis import Redis

from ops_ai.alerts import deliver_pending
from ops_ai.clock import system_clock
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory


def main() -> None:
    parser = argparse.ArgumentParser(description="Deliver pending ops alerts")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = load_ops_ai_settings()
    engine = make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    redis = Redis.from_url(settings.redis_url, socket_connect_timeout=1.0, socket_timeout=2.0)
    try:
        while True:
            delivered = deliver_pending(session_factory, redis, system_clock)
            if args.once:
                return
            if delivered == 0:
                time.sleep(1)
    finally:
        redis.close()
        engine.dispose()


if __name__ == "__main__":
    main()
