from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker


def check_ready(session_factory: sessionmaker[Session], redis: Redis) -> list[str]:
    """Names of the unreachable components ("database", "redis"); empty when ready. Never raises."""
    failed: list[str] = []
    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
    except Exception:
        failed.append("database")
    try:
        redis.ping()
    except Exception:
        failed.append("redis")
    return failed
