"""add search document embeddings (pgvector, one column + HNSW index per embedder)

Revision ID: b6e1c2d3f4a5
Revises: a3f7c9e15b28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b6e1c2d3f4a5"
down_revision: str | None = "a3f7c9e15b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_COLUMNS = (
    ("embedding_local_hash_v1", 512),
    ("embedding_openai_te3_small", 1536),
)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    for column, dimensions in EMBEDDING_COLUMNS:
        op.execute(f"ALTER TABLE ops_ai.search_documents ADD COLUMN {column} vector({dimensions})")
        op.execute(
            f"CREATE INDEX ix_search_documents_{column} ON ops_ai.search_documents "
            f"USING hnsw ({column} vector_cosine_ops)"
        )


def downgrade() -> None:
    # The extension stays: other schemas may use it, and dropping it is not needed to undo this revision.
    for column, _dimensions in reversed(EMBEDDING_COLUMNS):
        op.execute(f"DROP INDEX ops_ai.ix_search_documents_{column}")
        op.execute(f"ALTER TABLE ops_ai.search_documents DROP COLUMN {column}")
