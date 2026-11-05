"""Tests for ops_ai/search/indexing.py and the platform/kind scoping of the search service,
written from docs/specs/sq1.md section 10.1-10.2."""

import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.investigation.models import IncidentRecord
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import seed_embedded, stored_vector
from ops_ai.search.embedder import LocalHashEmbedder, document_text
from ops_ai.search.indexing import INCIDENT_KIND, PLATFORM_MERCHANT_ID, incident_ref_id, index_incidents
from ops_ai.search.service import hybrid_search, search_documents
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b2")
STARTED_AT = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
TITLE = "Issuer outage on BIN 400000"
ROOT_CAUSE = "Issuer returned timeouts on BIN 400000"
FIX = "Rerouted BIN 400000 to acquirer acq_b"
NOTES = "Failure rate back to baseline in 20 minutes"
QUERY = "issuer outage BIN 400000"


def _add_incident(
    session_factory: sessionmaker[Session],
    incident_id: str,
    merchant_id: uuid.UUID | None,
    *,
    title: str = TITLE,
    root_cause: str = ROOT_CAUSE,
    fix_applied: str = FIX,
    resolution_notes: str = NOTES,
) -> None:
    with session_factory() as session:
        session.add(
            IncidentRecord(
                incident_id=incident_id,
                merchant_id=merchant_id,
                title=title,
                root_cause=root_cause,
                fix_applied=fix_applied,
                resolution_notes=resolution_notes,
                started_at=STARTED_AT,
                resolved_at=STARTED_AT + timedelta(hours=3),
                recurrence_within_7d=False,
            )
        )
        session.commit()


def _index(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        count = index_incidents(session)
        session.commit()
    return count


def _incident_documents(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT kind, ref_id, merchant_id, title, body, occurred_at FROM ops_ai.search_documents "
                "WHERE kind = 'incident' ORDER BY title"
            )
        ).mappings()
        return [dict(row) for row in rows]


def _document_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM ops_ai.search_documents")).scalar_one())


def test_incident_ref_id_is_a_stable_uuid5_distinct_per_incident() -> None:
    # Arrange / Act
    first, again, other = (
        incident_ref_id("inc_0001"),
        incident_ref_id("inc_0001"),
        incident_ref_id("inc_0002"),
    )

    # Assert
    assert first == again
    assert first != other
    assert first.version == 5


def test_platform_merchant_id_is_the_nil_uuid() -> None:
    # Arrange / Act / Assert
    assert PLATFORM_MERCHANT_ID == uuid.UUID(int=0)
    assert INCIDENT_KIND == "incident"


def test_index_incidents_writes_one_incident_document_per_incident_and_returns_the_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_incident(session_factory, "inc_own", MERCHANT, title="A own incident")
    _add_incident(session_factory, "inc_platform", None, title="B platform incident")

    # Act
    count = _index(session_factory)

    # Assert
    assert count == 2
    assert _incident_documents(session_factory) == [
        {
            "kind": "incident",
            "ref_id": incident_ref_id("inc_own"),
            "merchant_id": MERCHANT,
            "title": "A own incident",
            "body": f"{ROOT_CAUSE} Fix: {FIX} Notes: {NOTES}",
            "occurred_at": STARTED_AT,
        },
        {
            "kind": "incident",
            "ref_id": incident_ref_id("inc_platform"),
            "merchant_id": PLATFORM_MERCHANT_ID,
            "title": "B platform incident",
            "body": f"{ROOT_CAUSE} Fix: {FIX} Notes: {NOTES}",
            "occurred_at": STARTED_AT,
        },
    ]


def test_index_incidents_stores_the_local_hash_vector_of_the_document_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_incident(session_factory, "inc_own", MERCHANT)
    expected = LocalHashEmbedder().embed([document_text(TITLE, f"{ROOT_CAUSE} Fix: {FIX} Notes: {NOTES}")])[0]

    # Act
    _index(session_factory)

    # Assert
    stored = stored_vector(session_factory, incident_ref_id("inc_own"), "local", kind="incident")
    assert stored is not None
    assert len(stored) == len(expected)
    assert max(abs(a - b) for a, b in zip(stored, expected, strict=True)) < 1e-6
    assert stored_vector(session_factory, incident_ref_id("inc_own"), "openai", kind="incident") is None


def test_index_incidents_cuts_the_title_to_300_characters(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _add_incident(session_factory, "inc_long", MERCHANT, title="x" * 400)

    # Act
    _index(session_factory)

    # Assert
    [document] = _incident_documents(session_factory)
    assert document["title"] == "x" * 300


def test_index_incidents_second_run_changes_no_row_count_and_updates_the_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_incident(session_factory, "inc_own", MERCHANT)
    _index(session_factory)
    with session_factory() as session:
        session.execute(
            text("UPDATE ops_ai.incidents SET title = 'Renamed incident' WHERE incident_id = 'inc_own'")
        )
        session.commit()

    # Act
    second = _index(session_factory)

    # Assert
    assert second == 1
    assert _document_count(session_factory) == 1
    assert [document["title"] for document in _incident_documents(session_factory)] == ["Renamed incident"]


def test_index_incidents_of_an_empty_table_returns_zero(session_factory: sessionmaker[Session]) -> None:
    # Arrange / Act
    count = _index(session_factory)

    # Assert
    assert count == 0
    assert _document_count(session_factory) == 0


def test_default_hybrid_search_never_returns_a_platform_incident_document(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_incident(session_factory, "inc_platform", None)
    _index(session_factory)

    # Act
    with session_factory() as session:
        hits = hybrid_search(session, Scope(merchant_id=MERCHANT), QUERY, LocalHashEmbedder())

    # Assert
    assert hits == []


def test_hybrid_search_with_platform_returns_own_and_platform_incidents_but_not_another_merchants(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_incident(session_factory, "inc_own", MERCHANT)
    _add_incident(session_factory, "inc_platform", None)
    _add_incident(session_factory, "inc_other", OTHER_MERCHANT)
    _index(session_factory)

    # Act
    with session_factory() as session:
        hits = hybrid_search(
            session,
            Scope(merchant_id=MERCHANT),
            QUERY,
            LocalHashEmbedder(),
            kinds=(INCIDENT_KIND,),
            include_platform=True,
        )

    # Assert
    assert {hit.ref_id for hit in hits} == {incident_ref_id("inc_own"), incident_ref_id("inc_platform")}
    assert {hit.kind for hit in hits} == {"incident"}


def test_kinds_filter_leaves_out_payment_documents_of_the_same_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _add_incident(session_factory, "inc_own", MERCHANT)
    _index(session_factory)
    seed_embedded(
        session_factory, MERCHANT, "Card payment declined", "card payment declined; BIN 400000 issuer outage"
    )

    # Act
    with session_factory() as session:
        filtered = hybrid_search(
            session, Scope(merchant_id=MERCHANT), QUERY, LocalHashEmbedder(), kinds=(INCIDENT_KIND,)
        )
        unfiltered = hybrid_search(session, Scope(merchant_id=MERCHANT), QUERY, LocalHashEmbedder())

    # Assert
    assert [hit.ref_id for hit in filtered] == [incident_ref_id("inc_own")]
    assert {hit.kind for hit in unfiltered} == {"incident", "payment"}


def test_include_platform_never_returns_a_non_incident_document_owned_by_the_platform_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seed_embedded(
        session_factory, PLATFORM_MERCHANT_ID, "Card payment declined", "card payment declined; issuer outage"
    )

    # Act
    with session_factory() as session:
        keyword = search_documents(session, Scope(merchant_id=MERCHANT), QUERY, include_platform=True)
        hybrid = hybrid_search(
            session, Scope(merchant_id=MERCHANT), QUERY, LocalHashEmbedder(), include_platform=True
        )

    # Assert
    assert keyword == []
    assert hybrid == []
