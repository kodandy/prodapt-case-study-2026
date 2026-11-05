"""DB tests of search_incidents_reranked, written from docs/specs/r2-agents.md section 2 (R2-4).

Hybrid first (keyword + vector legs fused with RRF), rerank second (recency, effectiveness, the scope
merchant's incident_feedback_scores). Incidents of one text are used wherever an order is asserted, so that
only the reranking signals (feedback, recurrence, age, resolution) can explain it.
"""

import uuid
from datetime import datetime, timedelta

import pytest
from ops_ai.scope import Scope
from ops_ai.search.__tests__.fake_embedders import RecordingEmbedder, UnavailableEmbedder
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.rerank import RankedIncident, search_incidents_reranked
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

TOLERANCE = 1e-9
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
QUERY = "acquirer timeout"
TITLE = "Acquirer timeout on card payments"
ROOT_CAUSE = "Connection pool exhausted at the acquirer"
FIX_APPLIED = "Raised the pool size"
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
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    incident_id: str,
    helpful: int,
    not_helpful: int,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_SCORE,
            {
                "merchant_id": merchant_id,
                "incident_id": incident_id,
                "helpful": helpful,
                "not_helpful": not_helpful,
                "updated_at": TEST_NOW,
            },
        )


def _search(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID = MERCHANT,
    query: str = QUERY,
    *,
    embedder: Embedder | None = None,
    limit: int | None = None,
) -> list[RankedIncident]:
    chosen = embedder if embedder is not None else LocalHashEmbedder()
    scope = Scope(merchant_id=merchant_id)
    with session_factory() as session:
        if limit is None:
            return search_incidents_reranked(session, scope, query, now=TEST_NOW, embedder=chosen)
        return search_incidents_reranked(session, scope, query, now=TEST_NOW, embedder=chosen, limit=limit)


def _ids(ranked: list[RankedIncident]) -> list[str]:
    return [item.incident_id for item in ranked]


# --- scope -------------------------------------------------------------------------------------------------


def test_another_merchants_incidents_never_appear_and_platform_wide_ones_do(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    _seed_incident(session_factory, "INC-PLATFORM", None)
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert sorted(_ids(ranked)) == ["INC-OWN", "INC-PLATFORM"]


def test_a_merchant_without_incidents_of_its_own_still_sees_the_platform_wide_ones(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-PLATFORM", None)
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-PLATFORM"]


def test_the_embedder_is_never_given_the_text_of_another_merchants_incident(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT, title="Acquirer timeout canary-77213")
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    ranked = _search(session_factory, embedder=embedder)

    # Assert
    sent = [sent_text for call in embedder.calls for sent_text in call]
    assert _ids(ranked) == ["INC-OWN"]
    assert QUERY in sent
    assert f"{TITLE} {ROOT_CAUSE} {FIX_APPLIED}" in sent
    assert [sent_text for sent_text in sent if "canary-77213" in sent_text] == []


def test_a_query_that_matches_no_visible_incident_returns_an_empty_list(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)
    _seed_incident(
        session_factory,
        "INC-UNRELATED",
        MERCHANT,
        title=UNRELATED_TITLE,
        root_cause=UNRELATED_ROOT_CAUSE,
        fix_applied=UNRELATED_FIX,
    )

    # Act
    ranked = _search(session_factory, query="zzqxv")

    # Assert
    assert ranked == []


# --- ordering ----------------------------------------------------------------------------------------------


def test_the_order_reflects_the_scope_merchants_feedback_scores_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: three incidents of one text; the other merchant's votes must not change the order
    for incident_id in ("INC-A", "INC-B", "INC-C"):
        _seed_incident(session_factory, incident_id, MERCHANT)
    _seed_score(session_factory, MERCHANT, "INC-C", helpful=5, not_helpful=0)
    _seed_score(session_factory, MERCHANT, "INC-B", helpful=0, not_helpful=5)
    _seed_score(session_factory, OTHER_MERCHANT, "INC-B", helpful=100, not_helpful=0)
    _seed_score(session_factory, OTHER_MERCHANT, "INC-C", helpful=0, not_helpful=100)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-C", "INC-A", "INC-B"]
    assert [item.effectiveness for item in ranked] == pytest.approx([6 / 7, 0.5, 1 / 7], abs=TOLERANCE)


def test_another_merchants_votes_on_a_platform_wide_incident_do_not_lift_it_for_this_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: both incidents are platform-wide; only the other merchant loves INC-P1
    _seed_incident(session_factory, "INC-P1", None)
    _seed_incident(session_factory, "INC-P2", None)
    _seed_score(session_factory, OTHER_MERCHANT, "INC-P1", helpful=50, not_helpful=0)
    _seed_score(session_factory, MERCHANT, "INC-P2", helpful=1, not_helpful=0)

    # Act
    ranked = {item.incident_id: item for item in _search(session_factory)}

    # Assert
    assert ranked["INC-P1"].effectiveness == pytest.approx(0.5, abs=TOLERANCE)
    assert ranked["INC-P2"].effectiveness == pytest.approx(2 / 3, abs=TOLERANCE)
    assert ranked["INC-P2"].score > ranked["INC-P1"].score


def test_a_recurred_incident_ranks_below_a_non_recurred_one_of_the_same_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the recurred one sorts first by id
    _seed_incident(session_factory, "INC-A", MERCHANT, recurrence=True)
    _seed_incident(session_factory, "INC-B", MERCHANT, recurrence=False)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert [item.effectiveness for item in ranked] == pytest.approx([0.5, 0.25], abs=TOLERANCE)


def test_a_recent_incident_ranks_above_an_older_one_of_the_same_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: the old one sorts first by id
    _seed_incident(
        session_factory,
        "INC-A",
        MERCHANT,
        started_at=TEST_NOW - timedelta(days=130),
        resolved_at=TEST_NOW - timedelta(days=120),
    )
    _seed_incident(session_factory, "INC-B", MERCHANT)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert ranked[1].recency == pytest.approx(0.5**4, abs=TOLERANCE)


def test_an_unresolved_incident_gets_zero_effectiveness_and_ranks_below_a_resolved_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-A", MERCHANT, resolved_at=None)
    _seed_incident(session_factory, "INC-B", MERCHANT)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert _ids(ranked) == ["INC-B", "INC-A"]
    assert (ranked[1].effectiveness, ranked[1].resolved_at) == (0.0, None)


def test_a_single_hit_carries_its_fields_and_the_exact_score(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: relevance 1.0 (the only candidate), recency 0.5 (resolved 30 days ago), effectiveness 0.5
    resolved_at = TEST_NOW - timedelta(days=30)
    started_at = TEST_NOW - timedelta(days=31)
    _seed_incident(session_factory, "INC-ONLY", MERCHANT, started_at=started_at, resolved_at=resolved_at)

    # Act
    (hit,) = _search(session_factory)

    # Assert
    assert (hit.incident_id, hit.title, hit.root_cause, hit.fix_applied) == (
        "INC-ONLY",
        TITLE,
        ROOT_CAUSE,
        FIX_APPLIED,
    )
    assert (hit.started_at, hit.resolved_at, hit.recurrence_within_7d) == (started_at, resolved_at, False)
    assert hit.relevance == pytest.approx(1.0, abs=TOLERANCE)
    assert hit.recency == pytest.approx(0.5, abs=TOLERANCE)
    assert hit.effectiveness == pytest.approx(0.5, abs=TOLERANCE)
    assert hit.score == pytest.approx(0.5 * 1.0 + 0.2 * 0.5 + 0.3 * 0.5, abs=TOLERANCE)


# --- limit -------------------------------------------------------------------------------------------------


def test_the_limit_caps_the_results_and_keeps_the_best_ranked(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    for incident_id in ("INC-1", "INC-2", "INC-3", "INC-4"):
        _seed_incident(session_factory, incident_id, MERCHANT)
    _seed_score(session_factory, MERCHANT, "INC-3", helpful=9, not_helpful=0)
    _seed_score(session_factory, MERCHANT, "INC-1", helpful=2, not_helpful=0)

    # Act
    ranked = _search(session_factory, limit=2)

    # Assert
    assert _ids(ranked) == ["INC-3", "INC-1"]


def test_the_default_limit_is_five(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    for number in range(7):
        _seed_incident(session_factory, f"INC-{number}", MERCHANT)

    # Act
    ranked = _search(session_factory)

    # Assert
    assert len(ranked) == 5


def test_a_limit_of_one_returns_the_single_best_incident(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-A", MERCHANT)
    _seed_incident(session_factory, "INC-B", MERCHANT)
    _seed_score(session_factory, MERCHANT, "INC-B", helpful=5, not_helpful=0)

    # Act
    ranked = _search(session_factory, limit=1)

    # Assert
    assert _ids(ranked) == ["INC-B"]


# --- degraded embedder -------------------------------------------------------------------------------------


def test_an_unavailable_embedder_still_returns_the_keyword_hits(
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

    # Act
    ranked = _search(session_factory, embedder=UnavailableEmbedder())

    # Assert
    assert _ids(ranked) == ["INC-MATCH"]
    assert ranked[0].relevance == pytest.approx(1.0, abs=TOLERANCE)
