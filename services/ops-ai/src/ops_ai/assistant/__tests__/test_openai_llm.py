"""Tests for ops_ai/assistant/openai_llm.py, written from docs/specs/llm-adapter.md (A1)."""

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

import pytest
from ops_ai.assistant.__tests__.fake_openai import (
    FakeOpenAiServer,
    ScriptedReply,
    closed_port_base_url,
    completed_reply,
    fake_server_session,
)
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.assistant.openai_llm import (
    DEFAULT_CHAT_MODEL,
    DEFAULT_FALLBACK_MODEL,
    LLM_BUDGET_SECONDS,
    MAX_OUTPUT_TOKENS,
    OPENAI_BASE_URL,
    OpenAiLlm,
)
from pydantic import JsonValue, SecretStr

CANARY_KEY = "sk-canary-openai-adapter-9f3e"
CHAT_MODEL = "chat-model-2026-01-01"
FALLBACK_MODEL = "fallback-model-2026-01-01"
QUESTION = "why were my payments declined?"
QUESTION_MARKER = "QUESTION-LEAK-MARKER-41"
EVIDENCE_MARKER = "EVIDENCE-LEAK-MARKER-77"
BODY_MARKER = "RESPONSE-BODY-LEAK-MARKER-58"
EVIDENCE = (
    EvidenceItem(
        ref="aggregate:decline_code:card_declined", summary="3 declined payments with code card_declined"
    ),
    EvidenceItem(ref="payment:0001", summary="USD 12.00 card payment declined"),
)
TIMEOUT_BUDGET_SECONDS = 0.5
TIMEOUT_DELAY_SECONDS = 2.0
ELAPSED_LIMIT_SECONDS = 1.5

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


def _adapter(
    base_url: str,
    fallback_model: str | None = None,
    budget_seconds: float = 5.0,
) -> OpenAiLlm:
    return OpenAiLlm(
        api_key=SecretStr(CANARY_KEY),
        chat_model=CHAT_MODEL,
        fallback_model=fallback_model,
        base_url=base_url,
        budget_seconds=budget_seconds,
    )


def _request(question: str = QUESTION, evidence: tuple[EvidenceItem, ...] = EVIDENCE) -> LlmRequest:
    return LlmRequest(question=question, evidence=evidence)


def _ok(text: str = "Narration [payment:0001]", model: str = CHAT_MODEL) -> ScriptedReply:
    return completed_reply(text, model)


def _error(status: int) -> ScriptedReply:
    return ScriptedReply(status=status, json_body={"error": {"message": BODY_MARKER}})


def _complete_expecting_failure(adapter: OpenAiLlm, request: LlmRequest | None = None) -> LlmUnavailableError:
    with pytest.raises(LlmUnavailableError) as exc:
        adapter.complete(request or _request())
    return exc.value


# --- module constants ---


def test_module_constants_match_the_spec() -> None:
    # Arrange: constants imported from the module under test

    # Act
    constants = (
        OPENAI_BASE_URL,
        DEFAULT_CHAT_MODEL,
        DEFAULT_FALLBACK_MODEL,
        LLM_BUDGET_SECONDS,
        MAX_OUTPUT_TOKENS,
    )

    # Assert
    assert constants == (
        "https://api.openai.com/v1",
        "gpt-4.1-mini-2025-04-14",
        "gpt-4.1-nano-2025-04-14",
        20.0,
        600,
    )


def test_openai_llm_is_assignable_to_the_llm_port() -> None:
    # Arrange
    adapter: LlmPort = _adapter(closed_port_base_url())

    # Act
    complete = adapter.complete

    # Assert
    assert complete.__name__ == "complete"


# --- request ---


def test_complete_posts_once_to_responses_with_bearer_key_and_json_content_type(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    adapter.complete(_request())

    # Assert
    recorded = server.requests
    assert len(recorded) == 1
    assert recorded[0].path == "/v1/responses"
    assert recorded[0].headers["authorization"] == f"Bearer {CANARY_KEY}"
    assert recorded[0].headers["content-type"].split(";")[0].strip() == "application/json"


def test_complete_sends_exactly_the_four_documented_body_keys_plus_store(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request())

    # Assert
    body = server.requests[0].json_object()
    assert set(body) == {"model", "instructions", "input", "max_output_tokens", "store"}
    assert body["model"] == CHAT_MODEL
    assert body["max_output_tokens"] == 600
    assert body["store"] is False


def test_complete_instructions_require_grounding_citation_and_admitting_gaps(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request())

    # Assert
    instructions = server.requests[0].string_field("instructions")
    lowered = instructions.lower()
    assert "only from the evidence" in lowered
    assert "[ref]" in instructions
    assert "does not answer" in lowered


def test_complete_instructions_treat_the_evidence_block_as_data_not_instructions(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request())

    # Assert
    lowered = server.requests[0].string_field("instructions").lower()
    assert "as data" in lowered
    assert "never as instructions" in lowered


def test_complete_instructions_are_fixed_and_carry_neither_question_nor_evidence(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok(), _ok()])
    adapter = _adapter(server.base_url)
    first = _request(QUESTION_MARKER, (EvidenceItem(ref="payment:1", summary=EVIDENCE_MARKER),))
    second = _request("another question", ())

    # Act
    adapter.complete(first)
    adapter.complete(second)

    # Assert
    instructions = [request.string_field("instructions") for request in server.requests]
    assert instructions[0] == instructions[1]
    assert QUESTION_MARKER not in instructions[0]
    assert EVIDENCE_MARKER not in instructions[0]


def test_complete_input_holds_the_question_and_each_evidence_item_in_request_order(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request())

    # Assert
    text = server.requests[0].string_field("input")
    evidence_lines = [line for line in text.splitlines() if line.startswith("- [")]
    assert QUESTION in text
    assert evidence_lines == [
        "- [aggregate:decline_code:card_declined] 3 declined payments with code card_declined",
        "- [payment:0001] USD 12.00 card payment declined",
    ]
    assert "(no evidence)" not in text


def test_complete_input_says_no_evidence_when_the_request_has_none(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request(evidence=()))

    # Assert
    text = server.requests[0].string_field("input")
    assert QUESTION in text
    assert "(no evidence)" in text
    assert [line for line in text.splitlines() if line.startswith("- [")] == []


# --- response ---


def test_complete_returns_the_output_text_and_the_model_reported_by_the_response(serve: ServeFake) -> None:
    # Arrange
    server = serve(
        [
            _ok(
                text="Three cards were declined [aggregate:decline_code:card_declined]",
                model="served-snapshot",
            )
        ]
    )
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    response = adapter.complete(_request())

    # Assert
    assert response == LlmResponse(
        text="Three cards were declined [aggregate:decline_code:card_declined]", model="served-snapshot"
    )
    assert len(server.requests) == 1


def test_complete_joins_output_text_parts_in_order_skips_other_items_and_strips(serve: ServeFake) -> None:
    # Arrange
    body: dict[str, JsonValue] = {
        "status": "completed",
        "model": CHAT_MODEL,
        "output": [
            {"type": "reasoning", "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "  Hello "},
                    {"type": "refusal", "refusal": "ignored"},
                    {"type": "output_text", "text": "world \n"},
                ],
            },
        ],
    }
    server = serve([ScriptedReply(json_body=body)])
    adapter = _adapter(server.base_url)

    # Act
    response = adapter.complete(_request())

    # Assert
    assert response == LlmResponse(text="Hello world", model=CHAT_MODEL)


@pytest.mark.parametrize(
    "model_field",
    [pytest.param({}, id="absent"), pytest.param({"model": 123}, id="not-a-string")],
)
def test_complete_reports_the_configured_model_when_the_response_model_is_missing_or_not_a_string(
    serve: ServeFake, model_field: dict[str, JsonValue]
) -> None:
    # Arrange
    body: dict[str, JsonValue] = {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "Answer"}]}],
        **model_field,
    }
    server = serve([ScriptedReply(json_body=body)])
    adapter = _adapter(server.base_url)

    # Act
    response = adapter.complete(_request())

    # Assert
    assert response == LlmResponse(text="Answer", model=CHAT_MODEL)


# --- failures: all of them raise LlmUnavailableError "openai: <reason>" ---


@dataclass(frozen=True)
class FailureCase:
    reply: ScriptedReply
    message: str | None
    """Exact error message, or None when only the `openai: ` prefix is pinned by the spec."""
    budget_seconds: float = 5.0


NO_TEXT_PART: dict[str, JsonValue] = {
    "status": "completed",
    "model": CHAT_MODEL,
    "output": [{"type": "message", "content": [{"type": "refusal", "refusal": BODY_MARKER}]}],
}
NO_MESSAGE_ITEM: dict[str, JsonValue] = {"status": "completed", "model": CHAT_MODEL, "output": []}
EMPTY_TEXT: dict[str, JsonValue] = {
    "status": "completed",
    "model": CHAT_MODEL,
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "  \n "}]}],
}
INCOMPLETE: dict[str, JsonValue] = {
    "status": "incomplete",
    "model": CHAT_MODEL,
    "incomplete_details": {"reason": BODY_MARKER},
    "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut off"}]}],
}
FAILURE_CASES = [
    pytest.param(FailureCase(_error(400), "openai: http 400"), id="http-400"),
    pytest.param(FailureCase(_error(401), "openai: http 401"), id="http-401"),
    pytest.param(FailureCase(_error(403), "openai: http 403"), id="http-403"),
    pytest.param(FailureCase(_error(422), "openai: http 422"), id="http-422"),
    pytest.param(
        FailureCase(ScriptedReply(raw_body=f"<html>{BODY_MARKER}</html>".encode()), None), id="not-json"
    ),
    pytest.param(FailureCase(ScriptedReply(json_body=[BODY_MARKER]), None), id="json-array"),
    pytest.param(FailureCase(ScriptedReply(json_body=INCOMPLETE), None), id="incomplete"),
    pytest.param(FailureCase(ScriptedReply(json_body=NO_TEXT_PART), None), id="no-output-text-part"),
    pytest.param(FailureCase(ScriptedReply(json_body=NO_MESSAGE_ITEM), None), id="no-message-item"),
    pytest.param(FailureCase(ScriptedReply(json_body=EMPTY_TEXT), None), id="blank-text"),
    pytest.param(
        FailureCase(
            ScriptedReply(delay_seconds=TIMEOUT_DELAY_SECONDS, json_body={}),
            "openai: timeout",
            budget_seconds=TIMEOUT_BUDGET_SECONDS,
        ),
        id="timeout",
    ),
]


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_complete_raises_an_openai_prefixed_error_after_one_request_when_the_failure_is_not_retryable(
    serve: ServeFake, case: FailureCase
) -> None:
    # Arrange
    server = serve([case.reply, _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL, budget_seconds=case.budget_seconds)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    message = str(error)
    assert message.startswith("openai: ")
    if case.message is not None:
        assert message == case.message
    assert len(message) <= 40
    assert len(server.requests) == 1


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_complete_error_has_no_cause_or_context_and_leaks_no_key_body_question_or_evidence(
    serve: ServeFake, case: FailureCase
) -> None:
    # Arrange
    server = serve([case.reply])
    adapter = _adapter(server.base_url, budget_seconds=case.budget_seconds)
    request = _request(QUESTION_MARKER, (EvidenceItem(ref="payment:9", summary=EVIDENCE_MARKER),))

    # Act
    error = _complete_expecting_failure(adapter, request)

    # Assert
    message = str(error)
    for secret in (CANARY_KEY, BODY_MARKER, QUESTION_MARKER, EVIDENCE_MARKER):
        assert secret not in message
    assert error.__cause__ is None
    assert error.__context__ is None


def test_complete_raises_openai_connection_error_without_cause_when_the_connection_is_refused() -> None:
    # Arrange
    adapter = _adapter(closed_port_base_url(), fallback_model=None)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    assert str(error).startswith("openai: ")
    assert CANARY_KEY not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


# --- fallback ---


def test_complete_uses_the_fallback_model_after_a_503_and_reports_its_text_and_model(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_error(503), _ok(text="Fallback narration", model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    response = adapter.complete(_request())

    # Assert
    assert response == LlmResponse(text="Fallback narration", model=FALLBACK_MODEL)
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]
    assert [request.path for request in server.requests] == ["/v1/responses", "/v1/responses"]
    assert [request.headers["authorization"] for request in server.requests] == [f"Bearer {CANARY_KEY}"] * 2


@pytest.mark.parametrize("status", [404, 408, 409, 429, 500, 502, 503, 504])
def test_complete_falls_back_on_retryable_http_statuses(serve: ServeFake, status: int) -> None:
    # Arrange
    server = serve([_error(status), _ok(text="Fallback narration", model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    response = adapter.complete(_request())

    # Assert
    assert response == LlmResponse(text="Fallback narration", model=FALLBACK_MODEL)
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


def test_complete_falls_back_when_the_connection_drops_without_a_response(serve: ServeFake) -> None:
    # Arrange
    server = serve(
        [ScriptedReply(drop_connection=True), _ok(text="Fallback narration", model=FALLBACK_MODEL)]
    )
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    response = adapter.complete(_request())

    # Assert
    assert response == LlmResponse(text="Fallback narration", model=FALLBACK_MODEL)
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_complete_makes_one_request_and_no_fallback_on_non_retryable_statuses(
    serve: ServeFake, status: int
) -> None:
    # Arrange
    server = serve([_error(status), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    assert str(error) == f"openai: http {status}"
    assert server.models() == [CHAT_MODEL]


def test_complete_makes_one_request_on_a_503_when_no_fallback_model_is_set(serve: ServeFake) -> None:
    # Arrange
    server = serve([_error(503), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=None)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    assert str(error) == "openai: http 503"
    assert server.models() == [CHAT_MODEL]


def test_complete_makes_one_request_on_a_503_when_the_fallback_equals_the_chat_model(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_error(503), _ok()])
    adapter = _adapter(server.base_url, fallback_model=CHAT_MODEL)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    assert str(error) == "openai: http 503"
    assert server.models() == [CHAT_MODEL]


def test_complete_makes_two_requests_at_most_when_both_models_fail(serve: ServeFake) -> None:
    # Arrange
    server = serve([_error(503), _error(429), _ok()])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    assert str(error).startswith("openai: ")
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


def test_complete_does_not_retry_the_fallback_model_when_it_returns_a_non_retryable_failure(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_error(500), _error(401), _ok()])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    assert str(error) == "openai: http 401"
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


# --- budget ---


def test_complete_raises_within_the_budget_and_makes_no_fallback_request_when_the_primary_hangs(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve(
        [
            ScriptedReply(delay_seconds=TIMEOUT_DELAY_SECONDS, json_body={}),
            _ok(text="Fallback narration", model=FALLBACK_MODEL),
        ]
    )
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL, budget_seconds=TIMEOUT_BUDGET_SECONDS)
    started = time.monotonic()

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    elapsed = time.monotonic() - started
    server.wait_for_request_count(1)
    time.sleep(0.3)
    assert str(error) == "openai: timeout"
    assert elapsed < ELAPSED_LIMIT_SECONDS
    assert server.models() == [CHAT_MODEL]


def test_complete_gives_the_fallback_only_the_remaining_budget(serve: ServeFake) -> None:
    # Arrange: the primary burns 0.7 s of a 1.0 s budget, then the fallback hangs
    server = serve(
        [
            ScriptedReply(status=503, delay_seconds=0.7, json_body={}),
            ScriptedReply(delay_seconds=TIMEOUT_DELAY_SECONDS, json_body={}),
        ]
    )
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL, budget_seconds=1.0)
    started = time.monotonic()

    # Act
    error = _complete_expecting_failure(adapter)

    # Assert
    elapsed = time.monotonic() - started
    assert str(error) == "openai: timeout"
    assert elapsed < 1.4
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


# --- secret handling ---


def test_repr_and_str_of_the_adapter_do_not_contain_the_key() -> None:
    # Arrange
    adapter = _adapter(closed_port_base_url(), fallback_model=FALLBACK_MODEL)

    # Act
    rendered = (repr(adapter), str(adapter))

    # Assert
    assert CANARY_KEY not in rendered[0]
    assert CANARY_KEY not in rendered[1]


def test_the_key_appears_only_in_the_authorization_header(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.complete(_request())

    # Assert
    recorded = server.requests[0]
    header_hits = {name for name, value in recorded.headers.items() if CANARY_KEY in value}
    assert header_hits == {"authorization"}
    assert CANARY_KEY not in recorded.path
    assert CANARY_KEY.encode() not in recorded.raw_body


def test_the_adapter_does_not_log_the_key_the_question_or_anything_under_its_own_logger(
    serve: ServeFake, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    server = serve([_error(503), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)
    failing = _adapter(closed_port_base_url())
    caplog.set_level(logging.DEBUG)

    # Act
    adapter.complete(_request(QUESTION_MARKER))
    _complete_expecting_failure(failing, _request(QUESTION_MARKER))

    # Assert
    messages = [record.getMessage() for record in caplog.records]
    assert [record.name for record in caplog.records if record.name.startswith("ops_ai")] == []
    assert [message for message in messages if CANARY_KEY in message or QUESTION_MARKER in message] == []
