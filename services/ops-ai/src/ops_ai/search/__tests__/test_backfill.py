import uuid

import pytest
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import seed_document, seed_embedded, stored_vector
from ops_ai.search.__tests__.fake_embedders import FixedVectorEmbedder, RecordingEmbedder, axis_vector
from ops_ai.search.backfill import backfill_embeddings
from ops_ai.search.embedder import LOCAL_DIMENSIONS, Embedder, LocalHashEmbedder, document_text
from ops_ai.search.openai_embedder import OPENAI_DIMENSIONS, OPENAI_EMBEDDER_ID
from ops_ai.search.service import vector_search
from sqlalchemy.orm import Session, sessionmaker

FLOAT4_TOLERANCE = 1e-6


def _expected(title: str, body: str) -> list[float]:
    return LocalHashEmbedder().embed([document_text(title, body)])[0]


def _backfill(session_factory: sessionmaker[Session], embedder: Embedder, batch_size: int = 100) -> int:
    with session_factory() as session:
        return backfill_embeddings(session, embedder, batch_size=batch_size)


def test_backfill_fills_the_null_local_cells_of_every_merchant_and_returns_the_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant_a = uuid.uuid4()
    merchant_b = uuid.uuid4()
    in_a = seed_document(session_factory, merchant_a, "Card payment declined", "insufficient funds")
    in_b = seed_document(session_factory, merchant_b, "Settlement payout delayed", "bank holiday")
    incident = seed_document(session_factory, merchant_b, "Gateway timeout", "upstream", kind="incident")

    # Act
    filled = _backfill(session_factory, LocalHashEmbedder())

    # Assert
    assert filled == 3
    assert stored_vector(session_factory, in_a, "local") == pytest.approx(
        _expected("Card payment declined", "insufficient funds"), abs=FLOAT4_TOLERANCE
    )
    assert stored_vector(session_factory, in_b, "local") == pytest.approx(
        _expected("Settlement payout delayed", "bank holiday"), abs=FLOAT4_TOLERANCE
    )
    assert stored_vector(session_factory, incident, "local", kind="incident") == pytest.approx(
        _expected("Gateway timeout", "upstream"), abs=FLOAT4_TOLERANCE
    )


def test_backfill_embeds_the_title_and_body_joined_by_document_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seed_document(session_factory, uuid.uuid4(), "Card payment declined", "insufficient funds")
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    _backfill(session_factory, embedder)

    # Assert
    assert embedder.calls == [["Card payment declined insufficient funds"]]


def test_backfill_leaves_cells_that_already_hold_a_vector_untouched(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    sentinel = axis_vector(LOCAL_DIMENSIONS, 7)
    already = seed_document(
        session_factory, merchant, "Card payment declined", "insufficient funds", local_vector=sentinel
    )
    missing = seed_document(session_factory, merchant, "Settlement payout delayed", "bank holiday")

    # Act
    filled = _backfill(session_factory, LocalHashEmbedder())

    # Assert
    assert filled == 1
    assert stored_vector(session_factory, already, "local") == pytest.approx(sentinel, abs=FLOAT4_TOLERANCE)
    assert stored_vector(session_factory, missing, "local") == pytest.approx(
        _expected("Settlement payout delayed", "bank holiday"), abs=FLOAT4_TOLERANCE
    )


def test_backfill_second_run_returns_zero_and_changes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ref_id = seed_document(session_factory, uuid.uuid4(), "Card payment declined", "insufficient funds")
    first_run = _backfill(session_factory, LocalHashEmbedder())
    after_first = stored_vector(session_factory, ref_id, "local")
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    second_run = _backfill(session_factory, embedder)

    # Assert
    assert first_run == 1
    assert second_run == 0
    assert embedder.calls == []
    assert stored_vector(session_factory, ref_id, "local") == after_first


def test_backfill_of_an_empty_table_returns_zero_without_embedding(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    filled = _backfill(session_factory, embedder)

    # Assert
    assert filled == 0
    assert embedder.calls == []


def test_backfill_of_the_local_embedder_does_not_touch_the_openai_column(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    sentinel = axis_vector(OPENAI_DIMENSIONS, 5)
    with_openai = seed_document(
        session_factory, merchant, "Card payment declined", "insufficient funds", openai_vector=sentinel
    )
    without_openai = seed_document(session_factory, merchant, "Settlement payout delayed", "bank holiday")

    # Act
    filled = _backfill(session_factory, LocalHashEmbedder())

    # Assert
    assert filled == 2
    assert stored_vector(session_factory, with_openai, "openai") == pytest.approx(
        sentinel, abs=FLOAT4_TOLERANCE
    )
    assert stored_vector(session_factory, without_openai, "openai") is None


def test_backfill_of_the_openai_embedder_fills_only_the_openai_column(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    local_sentinel = axis_vector(LOCAL_DIMENSIONS, 3)
    first = seed_document(
        session_factory,
        merchant,
        "Card payment declined",
        "insufficient funds",
        local_vector=local_sentinel,
    )
    second = seed_document(session_factory, merchant, "Settlement payout delayed", "bank holiday")
    embedder = FixedVectorEmbedder(
        vectors={
            "Card payment declined insufficient funds": axis_vector(OPENAI_DIMENSIONS, 1),
            "Settlement payout delayed bank holiday": axis_vector(OPENAI_DIMENSIONS, 2),
        },
        embedder_id=OPENAI_EMBEDDER_ID,
        dimensions=OPENAI_DIMENSIONS,
    )

    # Act
    filled = _backfill(session_factory, embedder)

    # Assert
    assert filled == 2
    assert stored_vector(session_factory, first, "openai") == pytest.approx(
        axis_vector(OPENAI_DIMENSIONS, 1), abs=FLOAT4_TOLERANCE
    )
    assert stored_vector(session_factory, second, "openai") == pytest.approx(
        axis_vector(OPENAI_DIMENSIONS, 2), abs=FLOAT4_TOLERANCE
    )
    assert stored_vector(session_factory, first, "local") == pytest.approx(
        local_sentinel, abs=FLOAT4_TOLERANCE
    )
    assert stored_vector(session_factory, second, "local") is None


def test_backfill_works_through_all_rows_in_batches_of_at_most_batch_size(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchants = [uuid.uuid4() for _ in range(3)]
    refs = [
        seed_document(session_factory, merchants[number % 3], f"Document {number}", f"body number {number}")
        for number in range(5)
    ]
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    filled = _backfill(session_factory, embedder, batch_size=2)

    # Assert
    assert filled == 5
    assert sorted(text for call in embedder.calls for text in call) == sorted(
        f"Document {number} body number {number}" for number in range(5)
    )
    assert max(len(call) for call in embedder.calls) <= 2
    for number, ref_id in enumerate(refs):
        assert stored_vector(session_factory, ref_id, "local") == pytest.approx(
            _expected(f"Document {number}", f"body number {number}"), abs=FLOAT4_TOLERANCE
        )


def test_backfilled_rows_are_found_by_the_vector_search(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    typo_doc = seed_document(session_factory, merchant, "Card payment declined", "insufficient funds")
    seed_document(session_factory, merchant, "Settlement payout delayed", "bank holiday")
    embedder = LocalHashEmbedder()
    with session_factory() as session:
        before = vector_search(session, Scope(merchant_id=merchant), "insufficent", embedder)
    _backfill(session_factory, embedder)

    # Act
    with session_factory() as session:
        after = vector_search(session, Scope(merchant_id=merchant), "insufficent", embedder)

    # Assert
    assert before == []
    assert [hit.ref_id for hit in after] == [typo_doc]


def test_backfill_keeps_rows_that_were_embedded_on_ingest_unchanged_and_counts_only_new_cells(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    embedded = seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")
    before = stored_vector(session_factory, embedded, "local")
    seed_document(session_factory, merchant, "Settlement payout delayed", "bank holiday")

    # Act
    filled = _backfill(session_factory, LocalHashEmbedder())

    # Assert
    assert filled == 1
    assert stored_vector(session_factory, embedded, "local") == before
