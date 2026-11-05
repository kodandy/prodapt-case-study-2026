"""The one write path into search_documents: payments (consumer) and incidents (seed / evaluation)."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ops_ai.investigation.models import IncidentRecord
from ops_ai.search.embedder import LocalHashEmbedder, document_text, is_zero
from ops_ai.search.models import SearchDocument

PLATFORM_MERCHANT_ID = uuid.UUID(int=0)
"""Owner of platform-wide incident documents (incidents.merchant_id NULL); merchant ids are never nil."""
INCIDENT_KIND = "incident"
TITLE_MAX_CHARS = 300
_INCIDENT_REF_NAMESPACE = uuid.UUID("0b5e7c1a-2d4f-4e6a-9c8b-7a1d3f5e9b20")
_LOCAL_EMBEDDER = LocalHashEmbedder()


def incident_ref_id(incident_id: str) -> uuid.UUID:
    return uuid.uuid5(_INCIDENT_REF_NAMESPACE, incident_id)


def upsert_search_document(
    session: Session,
    *,
    kind: str,
    ref_id: uuid.UUID,
    merchant_id: uuid.UUID,
    title: str,
    body: str,
    occurred_at: datetime,
) -> None:
    """Upserts on (kind, ref_id) with the local-hash-v1 vector (NULL for a token-less text). Other embedders'
    columns are filled by `python -m ops_ai.search backfill`. Does not commit."""
    [embedding] = _LOCAL_EMBEDDER.embed([document_text(title, body)])
    document = {
        "kind": kind,
        "ref_id": ref_id,
        "merchant_id": merchant_id,
        "title": title,
        "body": body,
        "occurred_at": occurred_at,
        "embedding_local_hash_v1": None if is_zero(embedding) else embedding,
    }
    session.execute(
        insert(SearchDocument)
        .values(**document)
        .on_conflict_do_update(index_elements=["kind", "ref_id"], set_=document)
    )


def index_incidents(session: Session) -> int:
    """Upserts one incident document per ops_ai.incidents row (all merchants). Returns the rows upserted.
    Does not commit."""
    incidents = session.execute(select(IncidentRecord).order_by(IncidentRecord.incident_id)).scalars().all()
    for incident in incidents:
        upsert_search_document(
            session,
            kind=INCIDENT_KIND,
            ref_id=incident_ref_id(incident.incident_id),
            merchant_id=incident.merchant_id or PLATFORM_MERCHANT_ID,
            title=incident.title[:TITLE_MAX_CHARS],
            body=f"{incident.root_cause} Fix: {incident.fix_applied} Notes: {incident.resolution_notes}",
            occurred_at=incident.started_at,
        )
    return len(incidents)
