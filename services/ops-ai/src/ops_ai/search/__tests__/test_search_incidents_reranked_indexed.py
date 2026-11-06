"""DB tests of search_incidents_reranked over the indexed incident documents, written from
docs/specs/r2-agents.md section 2.1 (R2-4 follow-up): stored vectors for indexed incidents, the pre-indexing
path only for un-indexed ones, tenant isolation re-checked against ops_ai.incidents.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from ops_ai.scope import Scope
from ops_ai.search.__tests__.fake_embedders import RecordingEmbedder, UnavailableEmbedder
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.indexing import INCIDENT_KIND, incident_ref_id, index_incidents, upsert_search_document
from ops_ai.search.rerank import RankedIncident, search_incidents_reranked
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

TOLERANCE = 1e-9
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c11")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c12")
QUERY = "acquirer timeout"
TITLE = "Acquirer timeout on card payments"
ROOT_CAUSE = "Connection pool exhausted at the acquirer"
FIX_APPLIED = "Raised the pool size"
INDEXED_FIX = "Raised the pool size twice"
CANARY_TITLE = "Acquirer timeout canary-58817"
UNRELATED_TITLE = "Settlement payout delayed"
UNRELATED_ROOT_CAUSE = "Bank holiday"
UNRELATED_FIX = "Waited for the next business day"

INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) "
    "VALUES (:incident_id, :merchant_id, :title, :root_cause, :fix_applied, 'notes', :started_at, "
    ":resolved_at, :recurrence)"
)
INSERT_SCORE = text(
    "INSERT INTO ops_ai.incident_feedback_scores "
    "(merchant_id, incident_id, helpful, not_helpful, updated_at) "
    "VALUES (:merchant_id, :incident_id, :helpful, :not_helpful, :updated_at)"
)


def _seed_incident(
    session_factory: sessionmaker[Session],
    incident_id: str,
    merchant_id: uuid.UUID | None,
    *,
    title: str = TITLE,
    root_cause: str = ROOT_CAUSE,
    fix_applied: str = FIX_APPLIED,
    started_at: datetime = TEST_NOW - timedelta(days=3),
    resolved_at: datetime | None = TEST_NOW - timedelta(days=2),
    recurrence: bool = False,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_INCIDENT,
            {
                "incident_id": incident_id,
                "merchant_id": merchant_id,
                "title": title,
                "root_cause": root_cause,
                "fix_applied": fix_applied,
                "started_at": started_at,
                "resolved_at": resolved_at,
                "recurrence": recurrence,
            },
        )


def _seed_score(
    session_factory: sessionmaker[Session], incident_id: str, helpful: int, not_helpful: int
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_SCORE,
            {
                "merchant_id": MERCHANT,
                "incident_id": incident_id,
                "helpful": helpful,
                "not_helpful": not_helpful,
                "updated_at": TEST_NOW,
            },
        )


def _index_all(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        index_incidents(session)


def _search(
    session_factory: sessionmaker[Session],
    *,
    embedder: Embedder | None = None,
    query: str = QUERY,
) -> list[RankedIncident]:
    chosen = embedder if embedder is not None else LocalHashEmbedder()
    with session_factory() as session:
        scope = Scope(merchant_id=MERCHANT)
        return search_incidents_reranked(session, scope, query, now=TEST_NOW, embedder=chosen)


def _ids(ranked: list[RankedIncident]) -> list[str]:
    return [item.incident_id for item in ranked]


def _sent_texts(embedder: RecordingEmbedder) -> list[str]:
    return [sent for call in embedder.calls for sent in call]


# --- stored vectors ----------------------------------------------------------------------------------------


def test_indexed_incidents_are_found_while_the_embedder_only_ever_receives_the_query(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    _seed_incident(session_factory, "INC-PLATFORM", None)
    _index_all(session_factory)
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    ranked = _search(session_factory, embedder=embedder)

    # Assert
    assert sorted(_ids(ranked)) == ["INC-OWN", "INC-PLATFORM"]
    assert embedder.calls != []
    assert {tuple(call) for call in embedder.calls} == {(QUERY,)}


def test_only_the_un_indexed_incidents_text_is_embedded_and_both_are_returned(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: INC-IDX is indexed, INC-NEW arrives after the indexing run
    _seed_incident(session_factory, "INC-IDX", MERCHANT, fix_applied=INDEXED_FIX)
    _index_all(session_factory)
    _seed_incident(session_factory, "INC-NEW", MERCHANT)
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    ranked = _search(session_factory, embedder=embedder)

    # Assert
    sent = _sent_texts(embedder)
    assert sorted(_ids(ranked)) == ["INC-IDX", "INC-NEW"]
    assert f"{TITLE} {ROOT_CAUSE} {FIX_APPLIED}" in sent
    assert [sent_text for sent_text in sent if INDEXED_FIX in sent_text] == []


# --- ordering on indexed incidents -------------------------------------------------------------------------


def test_feedback_scores_order_indexed_incidents_of_one_text(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    for incident_id in ("INC-A", "INC-B", "INC-C"):
        _seed_incident(session_factory, incident_id, MERCHANT)
    _index_all(session_factory)
    _seed_score(session_factory, "INC-C", helpful=5, not_helpful=0)
    _seed_score(session_factory, "INC-B", helpful=0, not_helpful=5)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-C", "INC-A", "INC-B"]
    assert [item.effectiveness for item in ranked] == pytest.approx([6 / 7, 0.5, 1 / 7], abs=TOLERANCE)


def test_a_recurred_indexed_incident_ranks_below_a_non_recurred_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the recurred one sorts first by id
    _seed_incident(session_factory, "INC-A", MERCHANT, recurrence=True)
    _seed_incident(session_factory, "INC-B", MERCHANT, recurrence=False)
    _index_all(session_factory)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert [item.effectiveness for item in ranked] == pytest.approx([0.5, 0.25], abs=TOLERANCE)


def test_a_single_indexed_hit_has_relevance_one_and_the_exact_score(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: recency 0.5 (resolved 30 days ago), effectiveness 0.5 (no votes)
    _seed_incident(
        session_factory,
        "INC-ONLY",
        MERCHANT,
        started_at=TEST_NOW - timedelta(days=31),
        resolved_at=TEST_NOW - timedelta(days=30),
    )
    _index_all(session_factory)

    # Act
    (hit,) = _search(session_factory)

    # Assert
    assert (hit.incident_id, hit.title, hit.fix_applied) == ("INC-ONLY", TITLE, FIX_APPLIED)
    assert hit.relevance == pytest.approx(1.0, abs=TOLERANCE)
    assert hit.score == pytest.approx(0.5 * 1.0 + 0.2 * 0.5 + 0.3 * 0.5, abs=TOLERANCE)


# --- tenant isolation --------------------------------------------------------------------------------------


def test_another_merchants_indexed_incident_of_the_same_text_never_appears(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    _seed_incident(session_factory, "INC-PLATFORM", None)
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)
    _index_all(session_factory)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert sorted(_ids(ranked)) == ["INC-OWN", "INC-PLATFORM"]


def test_a_document_owned_by_the_merchant_but_pointing_at_another_merchants_incident_is_dropped(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: a stale / mis-owned document must not widen visibility of INC-OTHER
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT, title=CANARY_TITLE)
    _index_all(session_factory)
    with session_factory.begin() as session:
        upsert_search_document(
            session,
            kind=INCIDENT_KIND,
            ref_id=incident_ref_id("INC-OTHER"),
            merchant_id=MERCHANT,
            title=TITLE,
            body=f"{ROOT_CAUSE} Fix: {FIX_APPLIED}",
            occurred_at=TEST_NOW - timedelta(days=3),
        )

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-OWN"]
    assert [item.title for item in ranked if "canary-58817" in item.title] == []


def test_a_merchant_with_only_foreign_indexed_incidents_gets_an_empty_list(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT, title=CANARY_TITLE)
    _index_all(session_factory)
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    ranked = _search(session_factory, embedder=embedder)

    # Assert
    assert ranked == []
    assert [sent_text for sent_text in _sent_texts(embedder) if "canary-58817" in sent_text] == []


# --- degraded embedder -------------------------------------------------------------------------------------


def test_an_unavailable_embedder_still_returns_the_indexed_keyword_hits(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-MATCH", MERCHANT)
    _seed_incident(
        session_factory,
        "INC-UNRELATED",
        MERCHANT,
        title=UNRELATED_TITLE,
        root_cause=UNRELATED_ROOT_CAUSE,
        fix_applied=UNRELATED_FIX,
    )
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)
    _index_all(session_factory)

    # Act
    ranked = _search(session_factory, embedder=UnavailableEmbedder())

    # Assert
    assert _ids(ranked) == ["INC-MATCH"]
    assert ranked[0].relevance == pytest.approx(1.0, abs=TOLERANCE)


def test_an_own_incident_whose_only_document_belongs_to_another_merchant_is_still_found(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the document of INC-OWN is mis-owned, so hybrid_search cannot return it for this scope
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    with session_factory.begin() as session:
        upsert_search_document(
            session,
            kind=INCIDENT_KIND,
            ref_id=incident_ref_id("INC-OWN"),
            merchant_id=OTHER_MERCHANT,
            title=TITLE,
            body=f"{ROOT_CAUSE} Fix: {FIX_APPLIED}",
            occurred_at=TEST_NOW - timedelta(days=3),
        )

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-OWN"]
