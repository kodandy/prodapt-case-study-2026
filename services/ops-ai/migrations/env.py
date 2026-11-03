import os

from alembic import context
from ops_ai.db import OPS_AI_SCHEMA
from ops_ai.schema import TARGET_METADATA
from sqlalchemy import create_engine, text


def run_migrations_online() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{OPS_AI_SCHEMA}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=TARGET_METADATA,
            version_table_schema=OPS_AI_SCHEMA,
            include_schemas=True,
            include_name=lambda name, type_, _parent: type_ != "schema" or name == OPS_AI_SCHEMA,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
