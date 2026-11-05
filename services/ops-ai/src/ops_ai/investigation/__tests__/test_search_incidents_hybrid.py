"""Tests for search_incidents on hybrid search, written from docs/specs/sq1.md section 10.3."""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ops_ai.investigation.models import IncidentRecord
from ops_ai.investigation.tools import search_incidents
from ops_ai.scope import Scope
from ops_ai.search.__tests__.fake_embedders import UnavailableEmbedder
from ops_ai.search.backfill import backfill_embeddings
from ops_ai.search.indexing import index_incidents
from ops_ai.search.openai_embedder import OPENAI_DIMENSIONS, OPENAI_EMBEDDER_ID
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000c1")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000c2")
SCOPE = Scope(merchant_id=MERCHANT)
STARTED_AT = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
QUERY = "issuer outage"
OUTAGE_WORDS = frozenset({"outage", "stalled", "unreachable"})
DISPUTE_WORDS = frozenset({"chargeback", "dispute"})
_WORD = re.compile(r"[a-z]+")


@dataclass
class ConceptEmbedder:
    """Stands in for a semantic model: texts naming the same concept get the same axis, whatever the words.

    Uses the OpenAI column so the vectors come from `backfill_embeddings`, the existing path for that column.
    """

    embedder_id: str = OPENAI_EMBEDDER_ID
    dimensions: int = OPENAI_DIMENSIONS
    calls: list[list[str]] = field(default_factory=list)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        words = set(_WORD.findall(text.lower()))
        vector = [0.0] * OPENAI_DIMENSIONS
        if words & OUTAGE_WORDS:
            vector[0] = 1.0
        elif words & DISPUTE_WORDS:
            vector[1] = 1.0
        return vector


def _add_incident(
    session_factory: sessionmaker[Session],
    incident_id: str,
    merchant_id: uuid.UUID | None,
    title: str,
    root_cause: str,
    fix_applied: str,
    *,
    resolved: bool = True,
) -> None:
    with session_factory() as session:
        session.add(
            IncidentRecord(
                incident_id=incident_id,
                merchant_id=merchant_id,
                title=title,
                root_cause=root_cause,
                fix_applied=fix_applied,
                resolution_notes="Closed after review",
                started_at=STARTED_AT,
                resolved_at=STARTED_AT + timedelta(hours=2) if resolved else None,
                recurrence_within_7d=False,
            )
        )
        session.commit()


def _keyword_incident(
    session_factory: sessionmaker[Session], incident_id: str, merchant_id: uuid.UUID | None
) -> None:
    _add_incident(
        session_factory,
        incident_id,
        merchant_id,
        "Issuer outage on BIN 400000",
        "Issuer returned timeouts",
        "Rerouted the BIN to acq_b",
    )


def _semantic_incident(
    session_factory: sessionmaker[Session], incident_id: str, merchant_id: uuid.UUID | None
) -> None:
    """Shares no keyword with QUERY (no 'issuer', no 'outage'), same concept for ConceptEmbedder."""
    _add_incident(
        session_factory,
        incident_id,
        merchant_id,
        "Card authorisations stalled at the network switch",
        "Switch unreachable from acquirer acq_a",
        "Failed over to the secondary switch",
    )


def _index_with(session_factory: sessionmaker[Session], embedder: ConceptEmbedder) -> None:
    with session_factory() as session:
        index_incidents(session)
        session.commit()
        backfill_embeddings(session, embedder)


def _ids(
    session_factory: sessionmaker[Session],
    embedder: ConceptEmbedder | UnavailableEmbedder | None,
    scope: Scope = SCOPE,
    query: str = QUERY,
) -> list[str]:
    with session_factory() as session:
        return [hit.incident_id for hit in search_incidents(session, scope, query, embedder=embedder)]


# --- semantic match --------------------------------------------------------------------------------------


def test_hybrid_search_incidents_returns_an_incident_worded_differently_from_the_query(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    embedder = ConceptEmbedder()
    _keyword_incident(session_factory, "inc_kw", MERCHANT)
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    _add_incident(
        session_factory, "inc_dispute", MERCHANT, "Chargeback wave", "Dispute backlog", "Added reviewers"
    )
    _index_with(session_factory, embedder)

    # Act
    hybrid = _ids(session_factory, embedder)
    keyword_only = _ids(session_factory, None)

    # Assert: the keyword match is in both legs and ranks first; the paraphrase comes from the vector leg only
    assert hybrid == ["inc_kw", "inc_sem"]
    assert keyword_only == ["inc_kw"]


def test_hybrid_search_incidents_embeds_the_query_with_the_given_embedder(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    embedder = ConceptEmbedder()
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    _index_with(session_factory, embedder)
    embedder.calls.clear()

    # Act
    _ids(session_factory, embedder)

    # Assert
    assert embedder.calls == [[QUERY]]


def test_hybrid_search_incidents_returns_the_hit_fields_from_the_incidents_table(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    embedder = ConceptEmbedder()
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    _index_with(session_factory, embedder)

    # Act
    with session_factory() as session:
        [hit] = search_incidents(session, SCOPE, QUERY, embedder=embedder)

    # Assert
    assert (hit.incident_id, hit.title, hit.root_cause, hit.fix_applied) == (
        "inc_sem",
        "Card authorisations stalled at the network switch",
        "Switch unreachable from acquirer acq_a",
        "Failed over to the secondary switch",
    )
    assert (hit.resolved_at, hit.recurrence_within_7d) == (STARTED_AT + timedelta(hours=2), False)


def test_hybrid_search_incidents_puts_resolved_incidents_before_unresolved_ones(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    embedder = ConceptEmbedder()
    _add_incident(
        session_factory,
        "inc_open",
        MERCHANT,
        "Issuer outage on BIN 400000",
        "Issuer outage ongoing",
        "Issuer outage escalated",
        resolved=False,
    )
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    _index_with(session_factory, embedder)

    # Act
    ids = _ids(session_factory, embedder)

    # Assert
    assert ids == ["inc_sem", "inc_open"]


def test_hybrid_search_incidents_returns_at_most_limit_hits(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    embedder = ConceptEmbedder()
    for index in range(4):
        _semantic_incident(session_factory, f"inc_sem_{index}", MERCHANT)
    _index_with(session_factory, embedder)

    # Act
    with session_factory() as session:
        hits = search_incidents(session, SCOPE, QUERY, limit=2, embedder=embedder)

    # Assert
    assert len(hits) == 2


# --- tenant isolation ------------------------------------------------------------------------------------


def test_hybrid_search_incidents_never_returns_another_merchants_incident(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the other merchant's incidents match both the keyword and the vector leg
    embedder = ConceptEmbedder()
    _keyword_incident(session_factory, "inc_other_kw", OTHER_MERCHANT)
    _semantic_incident(session_factory, "inc_other_sem", OTHER_MERCHANT)
    _semantic_incident(session_factory, "inc_own_sem", MERCHANT)
    _index_with(session_factory, embedder)

    # Act
    own = _ids(session_factory, embedder)
    other = _ids(session_factory, embedder, scope=Scope(merchant_id=OTHER_MERCHANT))

    # Assert
    assert own == ["inc_own_sem"]
    assert other == ["inc_other_kw", "inc_other_sem"]


def test_hybrid_search_incidents_returns_a_platform_wide_incident_found_by_the_vector_leg(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    embedder = ConceptEmbedder()
    _semantic_incident(session_factory, "inc_platform_sem", None)
    _index_with(session_factory, embedder)

    # Act
    ids = _ids(session_factory, embedder)

    # Assert
    assert ids == ["inc_platform_sem"]


def test_hybrid_search_incidents_drops_a_document_whose_incident_is_not_visible_to_the_scope(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: indexed as the merchant's, then the incident moved to another merchant without re-indexing
    embedder = ConceptEmbedder()
    _semantic_incident(session_factory, "inc_moved", MERCHANT)
    _index_with(session_factory, embedder)
    with session_factory() as session:
        incident = session.get(IncidentRecord, "inc_moved")
        assert incident is not None
        incident.merchant_id = OTHER_MERCHANT
        session.commit()

    # Act
    ids = _ids(session_factory, embedder)

    # Assert
    assert ids == []


# --- fallback --------------------------------------------------------------------------------------------


def test_search_incidents_without_an_embedder_is_the_incidents_table_full_text_search(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: nothing indexed in search_documents
    _keyword_incident(session_factory, "inc_kw", MERCHANT)
    _keyword_incident(session_factory, "inc_platform_kw", None)
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    _keyword_incident(session_factory, "inc_other_kw", OTHER_MERCHANT)

    # Act
    ids = _ids(session_factory, None)

    # Assert
    assert ids == ["inc_kw", "inc_platform_kw"]


def test_hybrid_search_incidents_still_finds_an_incident_that_is_not_indexed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: inserted after indexing, so it has no search document
    embedder = ConceptEmbedder()
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    _index_with(session_factory, embedder)
    _keyword_incident(session_factory, "inc_late", MERCHANT)

    # Act
    ids = _ids(session_factory, embedder)

    # Assert
    assert ids == ["inc_sem", "inc_late"]


def test_hybrid_search_incidents_with_an_unavailable_embedder_keeps_the_keyword_hits(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _keyword_incident(session_factory, "inc_kw", MERCHANT)
    _semantic_incident(session_factory, "inc_sem", MERCHANT)
    with session_factory() as session:
        index_incidents(session)
        session.commit()

    # Act
    ids = _ids(session_factory, UnavailableEmbedder())

    # Assert
    assert ids == ["inc_kw"]
