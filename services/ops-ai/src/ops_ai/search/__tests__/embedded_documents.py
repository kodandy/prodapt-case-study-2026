"""Seeding and reading search_documents rows together with their embedding columns."""

import uuid
from datetime import UTC, datetime
from typing import Literal

from ops_ai.search.embedder import LocalHashEmbedder, document_text
from ops_ai.search.models import SearchDocument
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
_FILLER = (
    "Payment reconciliation note. The acquirer batch for the weekly settlement run was retrieved overnight "
    "and the network response listed every card payment with its amount, currency, provider, BIN and decline "
    "code. Operations reviewed the minor unit amounts against the ledger and compared the totals with the "
    "previous period. "
)
NEEDLE_WORD = "chargeback"
NEEDLE_BODY = f"{_FILLER * 3}One entry contained a {NEEDLE_WORD} marker. {_FILLER}"
"""A ~200-word body holding one rare word: the keyword leg matches it, its cosine to that word is far
below DEFAULT_MIN_SIMILARITY (the tests still assert that premise)."""
EmbeddingColumn = Literal["local", "openai"]
_READ_QUERIES: dict[EmbeddingColumn, str] = {
    "local": "SELECT embedding_local_hash_v1::text AS v FROM ops_ai.search_documents "
    "WHERE kind = :kind AND ref_id = :ref_id",
    "openai": "SELECT embedding_openai_te3_small::text AS v FROM ops_ai.search_documents "
    "WHERE kind = :kind AND ref_id = :ref_id",
}


def seed_document(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    title: str,
    body: str,
    *,
    kind: str = "payment",
    local_vector: list[float] | None = None,
    openai_vector: list[float] | None = None,
    occurred_at: datetime = OCCURRED_AT,
) -> uuid.UUID:
    """Inserts one row with exactly the given embedding columns (None leaves the column NULL)."""
    ref_id = uuid.uuid4()
    with session_factory() as session:
        session.add(
            SearchDocument(
                kind=kind,
                ref_id=ref_id,
                merchant_id=merchant_id,
                title=title,
                body=body,
                occurred_at=occurred_at,
                embedding_local_hash_v1=local_vector,
                embedding_openai_te3_small=openai_vector,
            )
        )
        session.commit()
    return ref_id


def seed_embedded(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    title: str,
    body: str,
    *,
    kind: str = "payment",
    occurred_at: datetime = OCCURRED_AT,
) -> uuid.UUID:
    """Like seed_document, with the local-hash-v1 vector of document_text(title, body)."""
    vector = LocalHashEmbedder().embed([document_text(title, body)])[0]
    return seed_document(
        session_factory, merchant_id, title, body, kind=kind, local_vector=vector, occurred_at=occurred_at
    )


def stored_vector(
    session_factory: sessionmaker[Session],
    ref_id: uuid.UUID,
    column: EmbeddingColumn,
    kind: str = "payment",
) -> list[float] | None:
    """The stored embedding as floats (float4 precision), or None when the cell is NULL."""
    with session_factory() as session:
        raw = session.execute(text(_READ_QUERIES[column]), {"kind": kind, "ref_id": ref_id}).scalar_one()
    if raw is None:
        return None
    return [float(part) for part in str(raw).strip("[]").split(",")]
