"""R1-5: ask_grounded() answers only from relevant retrieved evidence, with per-sentence citations."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ops_ai.assistant.citations import CANNOT_ANSWER_TEXT, UnsupportedSentence
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError, MockLlm
from ops_ai.assistant.service import GroundedAssistantAnswer, ask, ask_grounded
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from ops_ai.search.__tests__.embedded_documents import seed_embedded
from sqlalchemy.orm import Session, sessionmaker

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
AGGREGATE_REF = "aggregate:decline_code:card_declined"
FOREIGN_REF = "payment:99999999-9999-9999-9999-999999999999"


@dataclass
class RecordingLlm:
    """Returns a fixed text and records every request."""

    text: str
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(text=self.text, model="fake-citing")


@dataclass
class UnavailableLlm:
    requests: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        raise LlmUnavailableError("provider outage")


def _seed_declines(session_factory: sessionmaker[Session], merchant_id: uuid.UUID, count: int) -> None:
    with session_factory() as session:
        for _ in range(count):
            session.add(
                PaymentFact(
                    payment_id=uuid.uuid4(),
                    merchant_id=merchant_id,
                    amount_minor=1500,
                    currency="USD",
                    method="card",
                    state="declined",
                    provider="acq_a",
                    decline_code="card_declined",
                    card_bin="400000",
                    entity_version=1,
                    occurred_at=OCCURRED_AT,
                )
            )
        session.commit()


def _ask_grounded(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    question: str,
    llm: MockLlm | RecordingLlm | UnavailableLlm,
) -> GroundedAssistantAnswer:
    with session_factory() as session:
        return ask_grounded(session, Scope(merchant_id=merchant_id), question, llm)


def test_mock_llm_answer_is_grounded_with_a_verbatim_excerpt_per_cited_sentence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 2)
    doc = seed_embedded(session_factory, merchant, "Card payment declined", "card_declined acq_a USD")

    # Act
    result = _ask_grounded(session_factory, merchant, "declined payments", MockLlm())

    # Assert
    evidence_by_ref = {item.ref: item.summary for item in result.answer.evidence}
    assert result.answer.mode == "llm"
    assert result.answer.model == "mock"
    assert result.grounding is not None
    assert result.grounding.grounded is True
    cited_refs = [source.ref for cited in result.grounding.citations for source in cited.sources]
    assert cited_refs == [AGGREGATE_REF, f"payment:{doc}"]
    for cited in result.grounding.citations:
        for source in cited.sources:
            assert source.excerpt
            assert source.excerpt in evidence_by_ref[source.ref]
    assert result.answer.text == "\n".join(cited.sentence for cited in result.grounding.citations)
    assert [(u.reason, u.refs) for u in result.grounding.unsupported] == [("no_citation", ())]


def test_grounded_evidence_equals_the_evidence_of_the_default_ask(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 1)
    seed_embedded(session_factory, merchant, "Card payment declined", "card_declined acq_a USD")
    seed_embedded(session_factory, merchant, "Settlement payout delayed", "bank holiday")
    with session_factory() as session:
        default = ask(session, Scope(merchant_id=merchant), "declined payments", MockLlm())

    # Act
    result = _ask_grounded(session_factory, merchant, "declined payments", MockLlm())

    # Assert
    assert result.answer.evidence == default.evidence


def test_unrelated_question_returns_cannot_answer_without_calling_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 3)
    llm = RecordingLlm(text="It will rain [aggregate:decline_code:card_declined].")

    # Act
    result = _ask_grounded(session_factory, merchant, "weather forecast tomorrow", llm)

    # Assert
    assert result.answer.mode == "llm"
    assert result.answer.text == CANNOT_ANSWER_TEXT
    assert result.answer.model is None
    assert result.grounding is not None
    assert (result.grounding.grounded, result.grounding.citations) == (False, ())
    assert result.relevant_refs == ()
    assert [item.ref for item in result.answer.evidence] == [AGGREGATE_REF]
    assert llm.requests == []


def test_merchant_with_no_records_gets_cannot_answer(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    llm = RecordingLlm(text="unused")

    # Act
    result = _ask_grounded(session_factory, uuid.uuid4(), "declined payments", llm)

    # Assert
    assert (result.answer.mode, result.answer.text, result.answer.evidence) == ("llm", CANNOT_ANSWER_TEXT, ())
    assert llm.requests == []


def test_llm_receives_require_citations_and_only_the_relevant_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 1)
    payout = seed_embedded(session_factory, merchant, "Settlement payout delayed", "bank holiday")
    llm = RecordingLlm(text=f"The payout was delayed by a bank holiday [payment:{payout}].")

    # Act
    result = _ask_grounded(session_factory, merchant, "payout delayed", llm)

    # Assert
    assert len(llm.requests) == 1
    assert llm.requests[0].require_citations is True
    assert [item.ref for item in llm.requests[0].evidence] == [f"payment:{payout}"]
    assert result.relevant_refs == (f"payment:{payout}",)
    assert AGGREGATE_REF in [item.ref for item in result.answer.evidence]


def test_a_sentence_citing_a_non_retrieved_ref_is_flagged_and_dropped(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 4)
    hallucinated = f"Another merchant lost 9000 USD [{FOREIGN_REF}]."
    llm = RecordingLlm(text=f"There are 4 declined payments [{AGGREGATE_REF}]. {hallucinated}")

    # Act
    result = _ask_grounded(session_factory, merchant, "declined payments", llm)

    # Assert
    assert result.answer.text == f"There are 4 declined payments [{AGGREGATE_REF}]."
    assert result.grounding is not None
    assert result.grounding.grounded is True
    assert result.grounding.unsupported == (
        UnsupportedSentence(sentence=hallucinated, reason="unknown_ref", refs=(FOREIGN_REF,)),
    )


def test_a_ref_retrieved_but_not_relevant_counts_as_not_retrieved_for_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 1)
    payout = seed_embedded(session_factory, merchant, "Settlement payout delayed", "bank holiday")
    llm = RecordingLlm(text=f"Declines are caused by the issuer [{AGGREGATE_REF}].")

    # Act
    result = _ask_grounded(session_factory, merchant, "payout delayed", llm)

    # Assert
    assert result.relevant_refs == (f"payment:{payout}",)
    assert result.answer.text == CANNOT_ANSWER_TEXT
    assert result.grounding is not None
    assert [(u.reason, u.refs) for u in result.grounding.unsupported] == [("unknown_ref", (AGGREGATE_REF,))]


def test_a_typo_question_is_relevant_through_the_vector_leg(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    merchant = uuid.uuid4()
    doc = seed_embedded(session_factory, merchant, "Card payment declined", "insufficient funds")

    # Act
    result = _ask_grounded(session_factory, merchant, "insufficent", MockLlm())

    # Assert
    assert result.relevant_refs == (f"payment:{doc}",)
    assert result.grounding is not None
    assert result.grounding.grounded is True
    assert [source.ref for cited in result.grounding.citations for source in cited.sources] == [
        f"payment:{doc}"
    ]


def test_llm_outage_falls_back_to_t2_with_no_text_and_no_grounding(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    _seed_declines(session_factory, merchant, 2)
    llm = UnavailableLlm()

    # Act
    result = _ask_grounded(session_factory, merchant, "declined payments", llm)

    # Assert
    assert (result.answer.mode, result.answer.text, result.answer.model) == ("t2_fallback", None, None)
    assert result.grounding is None
    assert result.relevant_refs == (AGGREGATE_REF,)
    assert len(llm.requests) == 1


def test_another_merchants_records_are_never_evidence(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    mine = uuid.uuid4()
    other = uuid.uuid4()
    _seed_declines(session_factory, other, 5)
    seed_embedded(session_factory, other, "Card payment declined", "card_declined acq_a USD")
    llm = RecordingLlm(text="unused")

    # Act
    result = _ask_grounded(session_factory, mine, "declined payments", llm)

    # Assert
    assert (result.answer.text, result.answer.evidence) == (CANNOT_ANSWER_TEXT, ())
    assert llm.requests == []
