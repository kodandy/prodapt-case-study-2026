"""R1-5: the OpenAI path of grounded /ask asks for [ref] citations; a cited non-retrieved id is dropped."""

import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime

import pytest
from ops_ai.assistant.__tests__.fake_openai import (
    FakeOpenAiServer,
    ScriptedReply,
    completed_reply,
    fake_server_session,
)
from ops_ai.assistant.citations import UnsupportedSentence
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest
from ops_ai.assistant.openai_llm import CITATION_RULE, INSTRUCTIONS, OpenAiLlm
from ops_ai.assistant.service import ask_grounded
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

CHAT_MODEL = "chat-model-2026-01-01"
AGGREGATE_REF = "aggregate:decline_code:card_declined"
FOREIGN_REF = "payment:99999999-9999-9999-9999-999999999999"
EVIDENCE = (EvidenceItem(ref=AGGREGATE_REF, summary="3 declined payments with decline code card_declined"),)

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


def _adapter(base_url: str) -> OpenAiLlm:
    return OpenAiLlm(
        api_key=SecretStr("sk-test-citations-not-a-key"),
        chat_model=CHAT_MODEL,
        fallback_model=None,
        base_url=base_url,
        budget_seconds=5.0,
    )


def test_require_citations_appends_the_citation_rule_to_the_instructions(serve: ServeFake) -> None:
    # Arrange
    server = serve([completed_reply(f"ok [{AGGREGATE_REF}]", CHAT_MODEL)])
    request = LlmRequest(question="declines", evidence=EVIDENCE, require_citations=True)

    # Act
    _adapter(server.base_url).complete(request)

    # Assert
    assert server.requests[0].string_field("instructions") == INSTRUCTIONS + CITATION_RULE


def test_without_require_citations_the_instructions_are_unchanged(serve: ServeFake) -> None:
    # Arrange
    server = serve([completed_reply("ok", CHAT_MODEL)])
    request = LlmRequest(question="declines", evidence=EVIDENCE)

    # Act
    _adapter(server.base_url).complete(request)

    # Assert
    assert server.requests[0].string_field("instructions") == INSTRUCTIONS


def test_grounded_ask_drops_an_openai_sentence_citing_a_non_retrieved_id(
    serve: ServeFake, session_factory: sessionmaker[Session]
) -> None:
    # Arrange
    merchant = uuid.uuid4()
    with session_factory() as session:
        for _ in range(3):
            session.add(
                PaymentFact(
                    payment_id=uuid.uuid4(),
                    merchant_id=merchant,
                    amount_minor=1500,
                    currency="USD",
                    method="card",
                    state="declined",
                    provider="acq_a",
                    decline_code="card_declined",
                    card_bin="400000",
                    entity_version=1,
                    occurred_at=datetime(2026, 11, 3, 11, 0, tzinfo=UTC),
                )
            )
        session.commit()
    hallucinated = f"A refund of 9000 USD is pending [{FOREIGN_REF}]."
    server = serve(
        [completed_reply(f"3 payments were declined [{AGGREGATE_REF}]. {hallucinated}", CHAT_MODEL)]
    )

    # Act
    with session_factory() as session:
        result = ask_grounded(
            session, Scope(merchant_id=merchant), "declined payments", _adapter(server.base_url)
        )

    # Assert
    assert result.answer.mode == "llm"
    assert result.answer.model == CHAT_MODEL
    assert result.answer.text == f"3 payments were declined [{AGGREGATE_REF}]."
    assert result.grounding is not None
    assert result.grounding.unsupported == (
        UnsupportedSentence(sentence=hallucinated, reason="unknown_ref", refs=(FOREIGN_REF,)),
    )
    assert server.requests[0].string_field("instructions") == INSTRUCTIONS + CITATION_RULE
