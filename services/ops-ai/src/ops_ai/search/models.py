import uuid
from datetime import datetime

from sqlalchemy import Computed, Index, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import InstrumentedAttribute, Mapped, mapped_column

from ops_ai.db import OpsAiBase
from ops_ai.search.embedder import LOCAL_DIMENSIONS, LOCAL_EMBEDDER_ID
from ops_ai.search.openai_embedder import OPENAI_DIMENSIONS, OPENAI_EMBEDDER_ID
from ops_ai.search.vector_type import Vector


class SearchDocument(OpsAiBase):
    """kind: payment | incident | resolution_note. One row per (kind, ref_id).

    One nullable vector column + HNSW cosine index per embedder id (see EMBEDDING_COLUMNS).
    """

    __tablename__ = "search_documents"
    __table_args__ = (
        Index("ix_search_documents_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_search_documents_embedding_local_hash_v1",
            "embedding_local_hash_v1",
            postgresql_using="hnsw",
            postgresql_ops={"embedding_local_hash_v1": "vector_cosine_ops"},
        ),
        Index(
            "ix_search_documents_embedding_openai_te3_small",
            "embedding_openai_te3_small",
            postgresql_using="hnsw",
            postgresql_ops={"embedding_openai_te3_small": "vector_cosine_ops"},
        ),
    )

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    ref_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', title || ' ' || body)", persisted=True)
    )
    occurred_at: Mapped[datetime]
    embedding_local_hash_v1: Mapped[list[float] | None] = mapped_column(Vector(LOCAL_DIMENSIONS))
    embedding_openai_te3_small: Mapped[list[float] | None] = mapped_column(Vector(OPENAI_DIMENSIONS))


EMBEDDING_COLUMNS: dict[str, InstrumentedAttribute[list[float] | None]] = {
    LOCAL_EMBEDDER_ID: SearchDocument.embedding_local_hash_v1,
    OPENAI_EMBEDDER_ID: SearchDocument.embedding_openai_te3_small,
}
EMBEDDING_DIMENSIONS: dict[str, int] = {
    LOCAL_EMBEDDER_ID: LOCAL_DIMENSIONS,
    OPENAI_EMBEDDER_ID: OPENAI_DIMENSIONS,
}


def embedding_column(embedder_id: str) -> InstrumentedAttribute[list[float] | None]:
    """Raises ValueError for an embedder id without a column."""
    column = EMBEDDING_COLUMNS.get(embedder_id)
    if column is None:
        raise ValueError(f"no embedding column for embedder {embedder_id!r}")
    return column
