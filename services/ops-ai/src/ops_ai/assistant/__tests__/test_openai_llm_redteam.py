"""Tests for ops_ai/assistant/openai_llm.py, from docs/specs/llm-adapter.md ("After the red-team run").

RT-001: any parse error stays inside the adapter. RT-002: rendering cost is bounded before any regex runs.
RT-003: card numbers in other layouts are masked.
"""

import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import pytest
from ops_ai.assistant.__tests__.fake_openai import (
    FakeOpenAiServer,
    ScriptedReply,
    completed_reply,
    fake_server_session,
)
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.assistant.openai_llm import OpenAiLlm
from pydantic import SecretStr

CANARY_KEY = "sk-canary-openai-redteam-6c2a"
CHAT_MODEL = "chat-model-2026-01-01"
FALLBACK_MODEL = "fallback-model-2026-01-01"
QUESTION = "why were my payments declined?"
MASK = "[card number removed]"
NARRATION = "Narration [payment:1]"
TAIL = "TAILMARKER"
COST_BUDGET_SECONDS = 1.0
COST_ELAPSED_LIMIT_SECONDS = 1.5
COST_ITEM_COUNT = 10
COST_PAYLOAD = "<ev" * 20000 + "idence>" * 20000
EVIDENCE_TAG = "<evidence>"

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


def _adapter(base_url: str, fallback_model: str | None = None, budget_seconds: float = 5.0) -> OpenAiLlm:
    return OpenAiLlm(
        api_key=SecretStr(CANARY_KEY),
        chat_model=CHAT_MODEL,
        fallback_model=fallback_model,
        base_url=base_url,
        budget_seconds=budget_seconds,
    )


def _request(question: str = QUESTION, evidence: tuple[EvidenceItem, ...] = ()) -> LlmRequest:
    return LlmRequest(question=question, evidence=evidence)


def _item(summary: str, ref: str = "payment:1") -> EvidenceItem:
    return EvidenceItem(ref=ref, summary=summary)


def _ok() -> ScriptedReply:
    return completed_reply(NARRATION, CHAT_MODEL)


def _sent_input(server: FakeOpenAiServer) -> str:
    return server.requests[0].string_field("input")


def _evidence_lines(sent_input: str) -> list[str]:
    return [line for line in sent_input.splitlines() if line.startswith("- [")]


@dataclass(frozen=True)
class BoundedRun:
    finished: bool
    outcome: LlmResponse | Exception | None


def _complete_within(adapter: OpenAiLlm, request: LlmRequest, limit_seconds: float) -> BoundedRun:
    """Runs `complete` in a daemon thread so a runaway render fails the test instead of hanging it."""
    outcomes: list[LlmResponse | Exception] = []

    def work() -> None:
        try:
            outcomes.append(adapter.complete(request))
        except Exception as error:
            outcomes.append(error)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout=limit_seconds)
    finished = not thread.is_alive()
    return BoundedRun(finished=finished, outcome=outcomes[0] if finished and outcomes else None)


# --- RT-001: any parse error stays inside the adapter ---

DEEP_ARRAY = b"[" * 20000
UNPARSEABLE_BODIES = [
    pytest.param(DEEP_ARRAY, id="20000-open-brackets"),
    pytest.param(b"[" * 60000, id="60000-open-brackets"),
    pytest.param(b'{"a":' * 20000, id="20000-nested-objects"),
    pytest.param(b"[" * 20000 + b"]" * 20000, id="20000-deep-valid-array"),
    pytest.param(b'{"status": "comp', id="truncated-object"),
    pytest.param(b"\xff\xfe\x00not json", id="invalid-utf8"),
    pytest.param(b"", id="empty-body"),
]


def test_complete_raises_invalid_json_for_a_deeply_nested_body_without_trying_the_fallback(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([ScriptedReply(raw_body=DEEP_ARRAY), _ok()])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    with pytest.raises(LlmUnavailableError) as exc:
        adapter.complete(_request())

    # Assert
    assert str(exc.value) == "openai: invalid json"
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert server.models() == [CHAT_MODEL]


@pytest.mark.parametrize("body", UNPARSEABLE_BODIES)
def test_complete_lets_only_llm_unavailable_error_leave_for_any_unparseable_200_body(
    serve: ServeFake, body: bytes
) -> None:
    # Arrange
    server = serve([ScriptedReply(raw_body=body)])
    adapter = _adapter(server.base_url)

    # Act
    outcome: LlmResponse | Exception
    try:
        outcome = adapter.complete(_request())
    except Exception as error:
        outcome = error

    # Assert
    assert isinstance(outcome, LlmUnavailableError)
    assert outcome.__cause__ is None
    assert outcome.__context__ is None
    assert len(server.requests) == 1


def test_complete_reports_invalid_json_not_invalid_body_for_a_deeply_nested_but_valid_array(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([ScriptedReply(raw_body=b"[" * 20000 + b"]" * 20000)])
    adapter = _adapter(server.base_url)

    # Act
    with pytest.raises(LlmUnavailableError) as exc:
        adapter.complete(_request())

    # Assert
    assert str(exc.value) == "openai: invalid json"


# --- RT-003: card numbers in other layouts ---

MASKED_RT003 = [
    pytest.param("4242.4242.4242.4242", id="dots"),
    pytest.param("4242/4242/4242/4242", id="slashes"),
    pytest.param("4242  4242  4242  4242", id="double-spaces"),
    pytest.param("4242_4242_4242_4242", id="underscores"),
    pytest.param("4242\t4242\t4242\t4242", id="tabs"),
    pytest.param("4242 -4242 -4242 -4242", id="space-then-hyphen"),
    pytest.param("4242-.4242-.4242-.4242", id="hyphen-then-dot"),
    pytest.param("00004242424242424242", id="20-digits-with-a-valid-16-digit-window"),
    pytest.param("0000 4242 4242 4242 4242", id="20-digits-grouped-with-a-valid-window"),
    pytest.param("4242424242424242" + "0000000000", id="26-digits-with-a-valid-window"),
]
KEPT_RT003 = [
    pytest.param("4242424242424241", id="16-digits-failing-luhn"),
    pytest.param("1234567890123", id="13-digits-failing-luhn"),
    pytest.param("2026-11-04", id="iso-date"),
    pytest.param("4242.4242.4242.4241", id="dots-failing-luhn"),
    pytest.param("1" * 21, id="21-digits-without-a-valid-window"),
]


@pytest.mark.parametrize("number", MASKED_RT003)
def test_complete_masks_a_luhn_valid_card_number_in_another_layout_in_the_summary(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    item = _item(f"card {number} was declined")

    # Act
    adapter.complete(_request(evidence=(item,)))

    # Assert
    assert _evidence_lines(_sent_input(server)) == [f"- [payment:1] card {MASK} was declined"]


@pytest.mark.parametrize("number", MASKED_RT003)
def test_complete_masks_a_luhn_valid_card_number_in_another_layout_in_the_question(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=f"my card {number} failed"))

    # Assert
    assert f"Question:\nmy card {MASK} failed\n" in _sent_input(server)


@pytest.mark.parametrize("number", KEPT_RT003)
def test_complete_leaves_a_candidate_that_is_not_a_card_number_unchanged_in_the_summary(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    item = _item(f"reference {number} was declined")

    # Act
    adapter.complete(_request(evidence=(item,)))

    # Assert
    assert _evidence_lines(_sent_input(server)) == [f"- [payment:1] reference {number} was declined"]


@pytest.mark.parametrize("number", KEPT_RT003)
def test_complete_leaves_a_candidate_that_is_not_a_card_number_unchanged_in_the_question(
    serve: ServeFake, number: str
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(question=f"reference {number} failed"))

    # Assert
    assert f"Question:\nreference {number} failed\n" in _sent_input(server)


def test_complete_keeps_the_punctuation_after_a_masked_card_number_in_another_layout(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    item = _item("tried 4242.4242.4242.4242. Next 5555/5555/5555/4444, then stopped")

    # Act
    adapter.complete(_request(evidence=(item,)))

    # Assert
    assert _evidence_lines(_sent_input(server)) == [f"- [payment:1] tried {MASK}. Next {MASK}, then stopped"]


def test_complete_never_sends_a_masked_card_number_in_another_layout_to_the_server(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    item = _item("4242.4242.4242.4242 and 4242_4242_4242_4242 and 00004242424242424242")

    # Act
    adapter.complete(_request(question="4242/4242/4242/4242", evidence=(item,)))

    # Assert
    raw = server.requests[0].raw_body.decode()
    assert [
        fragment for fragment in ("4242.4242", "4242_4242", "4242/4242", "00004242") if fragment in raw
    ] == []


def test_complete_does_not_mask_digits_in_a_ref(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    item = _item("settled", ref="4242424242424242")

    # Act
    adapter.complete(_request(evidence=(item,)))

    # Assert
    assert _evidence_lines(_sent_input(server)) == ["- [4242424242424242] settled"]


# --- RT-002: rendering cost is bounded before any regex runs ---


@pytest.mark.parametrize(
    ("field", "tags_kept_by_the_raw_cut"),
    [
        pytest.param("summary", 160, id="summary-1600"),
        pytest.param("ref", 48, id="ref-480"),
        pytest.param("question", 800, id="question-8000"),
    ],
)
def test_complete_drops_text_that_lies_beyond_four_times_the_final_limit_of_the_raw_field(
    serve: ServeFake, field: str, tags_kept_by_the_raw_cut: int
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    padded = EVIDENCE_TAG * tags_kept_by_the_raw_cut + TAIL
    request = _field_request(field, padded)

    # Act
    adapter.complete(request)

    # Assert
    sent = _sent_input(server)
    assert sent.count(TAIL) == 0
    assert sent.count(EVIDENCE_TAG) == 1


@pytest.mark.parametrize(
    ("field", "tags_before_the_raw_cut"),
    [
        pytest.param("summary", 159, id="summary-1600"),
        pytest.param("ref", 47, id="ref-480"),
        pytest.param("question", 799, id="question-8000"),
    ],
)
def test_complete_keeps_text_that_lies_within_four_times_the_final_limit_of_the_raw_field(
    serve: ServeFake, field: str, tags_before_the_raw_cut: int
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    padded = EVIDENCE_TAG * tags_before_the_raw_cut + TAIL
    request = _field_request(field, padded)

    # Act
    adapter.complete(request)

    # Assert
    sent = _sent_input(server)
    assert sent.count(TAIL) == 1
    assert sent.count(EVIDENCE_TAG) == 1


def _field_request(field: str, value: str) -> LlmRequest:
    if field == "question":
        return _request(question=value, evidence=(_item("plain"),))
    if field == "ref":
        return _request(evidence=(_item("plain", ref=value),))
    return _request(evidence=(_item(value),))


def test_complete_answers_within_the_budget_for_evidence_summaries_built_to_make_tag_removal_quadratic(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url, budget_seconds=COST_BUDGET_SECONDS)
    items = tuple(_item(COST_PAYLOAD, ref=f"payment:{index}") for index in range(COST_ITEM_COUNT))

    # Act
    run = _complete_within(adapter, _request(evidence=items), COST_ELAPSED_LIMIT_SECONDS)

    # Assert
    assert run.finished is True
    assert run.outcome == LlmResponse(text=NARRATION, model=CHAT_MODEL)
    sent = _sent_input(server)
    assert sent.count("<evidence>") == 1
    assert sent.count("</evidence>") == 1
    assert len(_evidence_lines(sent)) == COST_ITEM_COUNT


@pytest.mark.parametrize("field", ["ref", "question"])
def test_complete_answers_within_the_budget_for_a_ref_or_question_built_to_make_tag_removal_quadratic(
    serve: ServeFake, field: str
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url, budget_seconds=COST_BUDGET_SECONDS)
    request = _field_request(field, COST_PAYLOAD)

    # Act
    run = _complete_within(adapter, request, COST_ELAPSED_LIMIT_SECONDS)

    # Assert
    assert run.finished is True
    assert run.outcome == LlmResponse(text=NARRATION, model=CHAT_MODEL)
    sent = _sent_input(server)
    assert sent.count("<evidence>") == 1
    assert sent.count("</evidence>") == 1


def test_complete_renders_a_large_but_tag_free_input_without_exceeding_the_time_limit(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url, budget_seconds=COST_BUDGET_SECONDS)
    items = tuple(_item("4242 " * 40000, ref=f"payment:{index}") for index in range(COST_ITEM_COUNT))
    started = time.monotonic()

    # Act
    run = _complete_within(
        adapter, _request(question="4242 " * 40000, evidence=items), COST_ELAPSED_LIMIT_SECONDS
    )
    elapsed = time.monotonic() - started

    # Assert
    assert run.finished is True
    assert run.outcome == LlmResponse(text=NARRATION, model=CHAT_MODEL)
    assert elapsed < COST_ELAPSED_LIMIT_SECONDS
