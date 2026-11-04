"""Tests for ops_ai/assistant/openai_llm.py, from docs/specs/llm-adapter.md ("After the second red-team run").

RT-003R: each field is tag-stripped, whitespace-collapsed and masked in a fixed order until nothing changes,
so a card number split by newlines, tabs or a removed tag is still masked. The question is one line.
"""

import re
from collections.abc import Callable, Iterator, Sequence

import pytest
from ops_ai.assistant.__tests__.fake_openai import (
    FakeOpenAiServer,
    ScriptedReply,
    completed_reply,
    fake_server_session,
)
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest
from ops_ai.assistant.openai_llm import OpenAiLlm
from pydantic import SecretStr

CANARY_KEY = "sk-canary-openai-order-4d1e"
CHAT_MODEL = "chat-model-2026-01-01"
QUESTION = "why were my payments declined?"
MASK = "[card number removed]"
PAN = "4242424242424242"
NARRATION = "Narration [payment:1]"

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]

MASKED_ORDER = [
    pytest.param("4242\n4242\n4242\n4242", id="newlines"),
    pytest.param("4242\t\t\t4242\t\t\t4242\t\t\t4242", id="three-tabs"),
    pytest.param("4242 4242 4242 4242", id="spaces"),
    pytest.param("4242 - 4242 - 4242 - 4242", id="space-hyphen-space"),
    pytest.param("4242...4242...4242...4242", id="three-dots"),
    pytest.param("4242\u00a04242\u00a04242\u00a04242", id="no-break-spaces"),
    pytest.param("4242 4242<evidence> 4242 4242", id="tag-between-groups"),
    pytest.param("4242 4242 <evidence4242424242424242> 4242 4242", id="tag-carrying-digits"),
]
MASKED_ORDER_EXACT = [case for case in MASKED_ORDER if case.id != "tag-carrying-digits"]
TAG_CASES = [
    pytest.param("4242 4242<evidence> 4242 4242", id="tag-between-groups"),
    pytest.param("4242 4242 <evidence4242424242424242> 4242 4242", id="tag-carrying-digits"),
    pytest.param("4242 4242</evidence> 4242 4242", id="closing-tag-between-groups"),
    pytest.param("4242 <evidence>4242<evidence> 4242 4242 </evidence>", id="several-tags"),
]
KEPT_ORDER = [
    pytest.param("4242424242424241", id="16-digits-failing-luhn"),
    pytest.param("1234567890123", id="13-digits-failing-luhn"),
    pytest.param("2026-11-04", id="iso-date"),
]
ONE_LINE_QUESTIONS = [
    pytest.param("a\nb", "a b", id="newline"),
    pytest.param("a\r\nb", "a b", id="crlf"),
    pytest.param("a\n\n\n\nb", "a b", id="blank-lines"),
    pytest.param("a\tb", "a b", id="tab"),
    pytest.param("a\u00a0b", "a b", id="no-break-space"),
    pytest.param("a \t\n\u00a0 b", "a b", id="mixed-whitespace-run"),
    pytest.param("\n  a b  \n", "a b", id="leading-and-trailing-whitespace"),
]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


def _adapter(base_url: str) -> OpenAiLlm:
    return OpenAiLlm(
        api_key=SecretStr(CANARY_KEY),
        chat_model=CHAT_MODEL,
        fallback_model=None,
        base_url=base_url,
        budget_seconds=5.0,
    )


def _request(question: str = QUESTION, evidence: tuple[EvidenceItem, ...] = ()) -> LlmRequest:
    return LlmRequest(question=question, evidence=evidence)


def _item(summary: str) -> EvidenceItem:
    return EvidenceItem(ref="payment:1", summary=summary)


def _sent_input(server: FakeOpenAiServer) -> str:
    return server.requests[0].string_field("input")


def _evidence_lines(sent_input: str) -> list[str]:
    return [line for line in sent_input.splitlines() if line.startswith("- [")]


def _digits(text: str) -> str:
    return re.sub(r"[^0-9]", "", text)


# --- masked after whitespace collapse and tag removal ---


@pytest.mark.parametrize("number", MASKED_ORDER)
def test_complete_masks_a_card_number_split_by_whitespace_or_tags_in_the_summary(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(evidence=(_item(f"card {number} was declined"),)))

    # Assert
    sent = _sent_input(server)
    assert MASK in sent
    assert PAN not in _digits(sent)


@pytest.mark.parametrize("number", MASKED_ORDER)
def test_complete_masks_a_card_number_split_by_whitespace_or_tags_in_the_question(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=f"my card {number} failed"))

    # Assert
    sent = _sent_input(server)
    assert MASK in sent
    assert PAN not in _digits(sent)


@pytest.mark.parametrize("number", MASKED_ORDER_EXACT)
def test_complete_replaces_the_whole_split_card_number_with_one_mask_in_the_summary(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(evidence=(_item(f"card {number} was declined"),)))

    # Assert
    assert _evidence_lines(_sent_input(server)) == [f"- [payment:1] card {MASK} was declined"]


@pytest.mark.parametrize("number", MASKED_ORDER_EXACT)
def test_complete_replaces_the_whole_split_card_number_with_one_mask_in_the_question(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=f"my card {number} failed"))

    # Assert
    assert _sent_input(server).splitlines()[:3] == ["Question:", f"my card {MASK} failed", ""]


# --- the question is one line ---


@pytest.mark.parametrize(("question", "rendered"), ONE_LINE_QUESTIONS)
def test_complete_renders_the_question_on_one_line(serve: ServeFake, question: str, rendered: str) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=question))

    # Assert
    assert _sent_input(server).splitlines()[:3] == ["Question:", rendered, ""]


# --- kept as before ---


@pytest.mark.parametrize("number", KEPT_ORDER)
def test_complete_leaves_a_non_card_candidate_unchanged_in_the_summary_after_the_reordering(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(evidence=(_item(f"reference {number} was declined"),)))

    # Assert
    sent = _sent_input(server)
    assert _evidence_lines(sent) == [f"- [payment:1] reference {number} was declined"]
    assert MASK not in sent


@pytest.mark.parametrize("number", KEPT_ORDER)
def test_complete_leaves_a_non_card_candidate_unchanged_in_the_question_after_the_reordering(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=f"reference {number} failed"))

    # Assert
    sent = _sent_input(server)
    assert sent.splitlines()[:3] == ["Question:", f"reference {number} failed", ""]
    assert MASK not in sent


# --- tags stay out after the reordering ---


@pytest.mark.parametrize("value", TAG_CASES)
def test_complete_keeps_exactly_one_evidence_block_when_a_tag_splits_a_card_number_in_the_summary(
    serve: ServeFake, value: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(evidence=(_item(f"card {value} was declined"),)))

    # Assert
    sent = _sent_input(server)
    assert sent.count("<evidence>") == 1
    assert sent.count("</evidence>") == 1


@pytest.mark.parametrize("value", TAG_CASES)
def test_complete_keeps_exactly_one_evidence_block_when_a_tag_splits_a_card_number_in_the_question(
    serve: ServeFake, value: str
) -> None:
    # Arrange
    server = serve([completed_reply(NARRATION, CHAT_MODEL)])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=f"my card {value} failed", evidence=(_item("plain"),)))

    # Assert
    sent = _sent_input(server)
    assert sent.count("<evidence>") == 1
    assert sent.count("</evidence>") == 1
