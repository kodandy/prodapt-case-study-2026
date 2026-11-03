import os

from alembic import context
from gateway.db import GATEWAY_SCHEMA, SIMULATOR_SCHEMA
from gateway.schema import TARGET_METADATA
from sqlalchemy import create_engine, text

SCHEMAS = (GATEWAY_SCHEMA, SIMULATOR_SCHEMA)


def run_migrations_online() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as connection:
        for schema in SCHEMAS:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()
        context.configure(
            connection=connection,
            target_metadata=list(TARGET_METADATA),
            version_table_schema=GATEWAY_SCHEMA,
            include_schemas=True,
            include_name=lambda name, type_, _parent: type_ != "schema" or name in SCHEMAS,
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
