"""ask() collects its search evidence from hybrid_search (keyword + vector, fused by RRF)."""

import uuid

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.assistant.service import AssistantAnswer, ask
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import NEEDLE_BODY, NEEDLE_WORD, seed_embedded
from ops_ai.search.__tests__.fake_embedders import RecordingEmbedder, UnavailableEmbedder
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.service import search_documents
from sqlalchemy.orm import Session, sessionmaker

TYPO_QUESTION = "insufficent"
TYPO_TITLE = "Card payment declined"
TYPO_BODY = "insufficient funds"
TIMEOUT_TITLE = "Card payment"
TIMEOUT_BODY = "gateway reported a timeout once"
NEEDLE_QUESTION = NEEDLE_WORD


def _ask(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    question: str,
    embedder: Embedder | None = None,
) -> AssistantAnswer:
    with session_factory() as session:
        return ask(session, Scope(merchant_id=merchant_id), question, MockLlm(), embedder)


def _keyword_refs(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, query: str
) -> list[uuid.UUID]:
    with session_factory() as session:
        return [hit.ref_id for hit in search_documents(session, Scope(merchant_id=merchant_id), query)]


def test_ask_without_an_embedder_adds_a_document_that_only_the_vector_leg_finds(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    typo_doc = seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    seed_embedded(session_factory, merchant, "Settlement payout delayed", "bank holiday")
    assert _keyword_refs(session_factory, merchant, TYPO_QUESTION) == []

    # Act
    answer = _ask(session_factory, merchant, TYPO_QUESTION)

    # Assert
    assert answer.mode == "llm"
    assert answer.evidence == (EvidenceItem(ref=f"payment:{typo_doc}", summary=f"{TYPO_TITLE}: {TYPO_BODY}"),)


def test_ask_keeps_a_document_that_only_the_keyword_leg_finds(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    needle_doc = seed_embedded(session_factory, merchant, TYPO_TITLE, NEEDLE_BODY)
    assert _keyword_refs(session_factory, merchant, NEEDLE_QUESTION) == [needle_doc]

    # Act
    answer = _ask(session_factory, merchant, NEEDLE_QUESTION)

    # Assert
    assert answer.evidence == (
        EvidenceItem(ref=f"payment:{needle_doc}", summary=f"{TYPO_TITLE}: {NEEDLE_BODY}"),
    )


def test_ask_embeds_the_question_with_the_embedder_it_is_given(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    typo_doc = seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    answer = _ask(session_factory, merchant, TYPO_QUESTION, embedder)

    # Assert
    assert embedder.calls == [[TYPO_QUESTION]]
    assert [item.ref for item in answer.evidence] == [f"payment:{typo_doc}"]


def test_ask_still_answers_with_keyword_evidence_when_the_embedder_is_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    timeout_doc = seed_embedded(session_factory, merchant, TIMEOUT_TITLE, TIMEOUT_BODY)
    seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    embedder = UnavailableEmbedder()

    # Act
    answer = _ask(session_factory, merchant, "timeout", embedder)

    # Assert
    assert (answer.mode, answer.model) == ("llm", "mock")
    assert answer.evidence == (
        EvidenceItem(ref=f"payment:{timeout_doc}", summary=f"{TIMEOUT_TITLE}: {TIMEOUT_BODY}"),
    )
    assert embedder.calls == [["timeout"]]


def test_ask_with_an_unavailable_embedder_leaves_out_documents_only_the_vector_leg_finds(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)

    # Act
    answer = _ask(session_factory, merchant, TYPO_QUESTION, UnavailableEmbedder())

    # Assert
    assert answer.mode == "llm"
    assert answer.evidence == ()


def test_ask_never_adds_another_merchants_document_through_the_vector_leg(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    mine = uuid.uuid4()
    theirs = uuid.uuid4()
    own_doc = seed_embedded(session_factory, mine, TYPO_TITLE, TYPO_BODY)
    seed_embedded(session_factory, theirs, TYPO_TITLE, TYPO_BODY)

    # Act
    answer = _ask(session_factory, mine, TYPO_QUESTION)

    # Assert
    assert [item.ref for item in answer.evidence] == [f"payment:{own_doc}"]


def test_ask_for_a_blank_question_has_no_search_evidence_and_does_not_embed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    seed_embedded(session_factory, merchant, TYPO_TITLE, TYPO_BODY)
    embedder = RecordingEmbedder(LocalHashEmbedder())

    # Act
    answer = _ask(session_factory, merchant, "   ", embedder)

    # Assert
    assert answer.evidence == ()
    assert embedder.calls == []
