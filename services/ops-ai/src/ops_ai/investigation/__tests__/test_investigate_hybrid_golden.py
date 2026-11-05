"""SQ1 on hybrid incident search over the generated dataset (docs/specs/sq1.md sections 10.3-10.5)."""

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.db import make_session_factory
from ops_ai.investigation.service import Investigation, investigate
from ops_ai.investigation.tools import search_incidents
from ops_ai.scope import Scope
from ops_ai.search.__tests__.fake_embedders import RecordingEmbedder
from ops_ai.search.embedder import LocalHashEmbedder
from ops_ai.search.indexing import PLATFORM_MERCHANT_ID, incident_ref_id, index_incidents
from ops_ai.seed import merchant_uuid, seed_dataset
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import DATA_SCHEMAS
from scripts.generate_dataset import generate

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
QUESTION = "15% of my card payments are failing this afternoon - what is the root cause and how do we fix it?"
SQ1_MERCHANT = "mch_0001"
OTHER_MERCHANT = "mch_0002"
SQ1_INCIDENT_QUERY = "BIN 400000 issuer"


@dataclass(frozen=True)
class Seeded:
    session_factory: sessionmaker[Session]
    indexed: int
    incidents: int


@pytest.fixture(scope="module")
def seeded(engine: Engine) -> Seeded:
    """generate(seed=42) seeded at ANCHOR, then its incidents indexed into search_documents."""
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) AND tablename <> 'alembic_version'"
            ),
            {"schemas": list(DATA_SCHEMAS)},
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    session_factory = make_session_factory(engine)
    log = generate(seed=42)
    seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR)
    with session_factory() as session:
        indexed = index_incidents(session)
        session.commit()
    return Seeded(session_factory=session_factory, indexed=indexed, incidents=len(log.incidents))


def _investigate(seeded: Seeded, embedder: RecordingEmbedder, merchant: str = SQ1_MERCHANT) -> Investigation:
    with seeded.session_factory() as session:
        return investigate(
            session,
            Scope(merchant_id=merchant_uuid(merchant)),
            QUESTION,
            ANCHOR,
            MockLlm(),
            embedder=embedder,
        )


def test_every_seeded_incident_is_indexed_with_its_merchant_or_the_platform_id(seeded: Seeded) -> None:
    # Arrange
    query = text(
        "SELECT merchant_id FROM ops_ai.search_documents WHERE kind = 'incident' AND ref_id = :ref_id"
    )

    # Act
    with seeded.session_factory() as session:
        sq1_owner = session.execute(query, {"ref_id": incident_ref_id("inc_0001")}).scalar_one()
        platform_owner = session.execute(query, {"ref_id": incident_ref_id("inc_0002")}).scalar_one()

    # Assert
    assert seeded.indexed == seeded.incidents
    assert sq1_owner == merchant_uuid(SQ1_MERCHANT)
    assert platform_owner == PLATFORM_MERCHANT_ID


def test_sq1_with_an_embedder_still_cites_inc_0001_and_its_acq_b_fix(seeded: Seeded) -> None:
    # Arrange
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    result = _investigate(seeded, embedder)

    # Assert
    refs = [item.ref for item in result.evidence]
    assert result.cause_label == "issuer_outage_on_bin"
    assert "incident:inc_0001" in refs
    assert "acq_b" in result.suggested_fixes[0]
    assert [call.name for call in result.tool_calls] == [
        "aggregate_failures",
        "compare_baseline",
        "search_incidents",
    ]


def test_sq1_incident_search_goes_through_the_embedder_with_the_incident_query(seeded: Seeded) -> None:
    # Arrange
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    _investigate(seeded, embedder)

    # Assert
    assert embedder.calls == [[SQ1_INCIDENT_QUERY]]


def test_sq1_hybrid_incident_search_ranks_inc_0001_first(seeded: Seeded) -> None:
    # Arrange
    scope = Scope(merchant_id=merchant_uuid(SQ1_MERCHANT))

    # Act
    with seeded.session_factory() as session:
        hits = search_incidents(session, scope, SQ1_INCIDENT_QUERY, embedder=LocalHashEmbedder())

    # Assert
    assert hits[0].incident_id == "inc_0001"


def test_sq1_with_an_embedder_never_shows_inc_0001_to_another_merchant(seeded: Seeded) -> None:
    # Arrange
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    result = _investigate(seeded, embedder, merchant=OTHER_MERCHANT)
    with seeded.session_factory() as session:
        hits = search_incidents(
            session,
            Scope(merchant_id=merchant_uuid(OTHER_MERCHANT)),
            SQ1_INCIDENT_QUERY,
            embedder=LocalHashEmbedder(),
        )

    # Assert
    assert "incident:inc_0001" not in [item.ref for item in result.evidence]
    assert "inc_0001" not in [hit.incident_id for hit in hits]
