import uuid

from sqlalchemy import select, tuple_, update
from sqlalchemy.orm import Session

from ops_ai.search.embedder import Embedder, document_text, is_zero
from ops_ai.search.models import SearchDocument, embedding_column

DEFAULT_BATCH_SIZE = 100


def backfill_embeddings(session: Session, embedder: Embedder, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """Fills NULL cells of the embedder's own column for every row (all merchants), committing per batch
    (keyset order on (kind, ref_id)). Returns rows filled; rows whose text has no tokens stay NULL.
    EmbedderUnavailableError propagates; batches already committed stay.
    """
    column = embedding_column(embedder.embedder_id)
    filled = 0
    after: tuple[str, uuid.UUID] | None = None
    while True:
        query = select(
            SearchDocument.kind, SearchDocument.ref_id, SearchDocument.title, SearchDocument.body
        ).where(column.is_(None))
        if after is not None:
            query = query.where(tuple_(SearchDocument.kind, SearchDocument.ref_id) > tuple_(*after))
        rows = session.execute(
            query.order_by(SearchDocument.kind, SearchDocument.ref_id).limit(batch_size)
        ).all()
        if not rows:
            return filled
        vectors = embedder.embed([document_text(row.title, row.body) for row in rows])
        for row, vector in zip(rows, vectors, strict=True):
            if is_zero(vector):
                continue
            session.execute(
                update(SearchDocument)
                .where(SearchDocument.kind == row.kind, SearchDocument.ref_id == row.ref_id)
                .values({column.key: vector})
            )
            filled += 1
        session.commit()
        after = (rows[-1].kind, rows[-1].ref_id)
