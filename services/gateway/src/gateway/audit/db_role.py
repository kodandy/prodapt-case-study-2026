import re

import psycopg
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import ConnectionPoolEntry

_ROLE_PATTERN = re.compile(r"[a-z_][a-z0-9_]{0,62}")


def use_db_role(engine: Engine, role: str) -> None:
    """Every new pooled connection runs `SET ROLE <role>`, so the app works with the least-privilege role
    (no UPDATE/DELETE on the audit log) whatever user the URL logs in as."""
    if _ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("invalid database role name")
    statement = f'SET ROLE "{role}"'

    @event.listens_for(engine, "connect")
    def _set_role(dbapi_connection: psycopg.Connection[object], _record: ConnectionPoolEntry) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute(statement.encode())
        dbapi_connection.commit()
