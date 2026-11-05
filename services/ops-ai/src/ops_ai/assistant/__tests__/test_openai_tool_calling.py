"""Tests for ops_ai/assistant/openai_tool_calling.py, written from docs/specs/failure-tools.md section 2."""

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
from ops_ai.assistant.llm_port import LlmUnavailableError
from ops_ai.assistant.openai_tool_calling import OpenAiToolCallingLlm
from ops_ai.assistant.tool_calling import ToolCallingLlm, ToolChoice, ToolChoiceRequest, ToolPlan, ToolSpec
from pydantic import JsonValue, SecretStr

CANARY_KEY = "sk-canary-openai-tool-calling-5d1c"
CHAT_MODEL = "chat-model-2026-01-01"
FALLBACK_MODEL = "fallback-model-2026-01-01"
QUESTION = "why were my card payments declined this morning?"
QUESTION_MARKER = "QUESTION-LEAK-MARKER-63"
BODY_MARKER = "RESPONSE-BODY-LEAK-MARKER-12"
INSTRUCTIONS = "Pick the tools that answer the question. Never invent tool names."
MASK = "[card number removed]"
KEY_ERROR = "OPENAI_API_KEY has invalid characters"
BASE_URL_ERROR = "OPENAI_BASE_URL must be https without userinfo, query or fragment (http only for loopback)"

AGGREGATE_SPEC = ToolSpec(
    name="aggregate_failures",
    description="Failure counts for a window.",
    parameters={
        "type": "object",
        "properties": {"lookback_hours": {"type": "integer", "minimum": 1, "maximum": 720}},
        "additionalProperties": False,
    },
)
INCIDENT_SPEC = ToolSpec(
    name="search_incidents",
    description="Search past incidents.",
    parameters={
        "type": "object",
        "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["query"],
        "additionalProperties": False,
    },
)
EXPECTED_BODY_KEYS = {"model", "instructions", "input", "tools", "tool_choice", "max_output_tokens", "store"}

ServeFake = Callable[[Sequence[ScriptedReply]], FakeOpenAiServer]


@pytest.fixture
def serve() -> Iterator[ServeFake]:
    yield from fake_server_session()


def _adapter(
    base_url: str,
    fallback_model: str | None = None,
    budget_seconds: float = 5.0,
    api_key: str = CANARY_KEY,
) -> OpenAiToolCallingLlm:
    return OpenAiToolCallingLlm(
        api_key=SecretStr(api_key),
        chat_model=CHAT_MODEL,
        fallback_model=fallback_model,
        base_url=base_url,
        budget_seconds=budget_seconds,
    )


def _request(
    question: str = QUESTION, tools: tuple[ToolSpec, ...] = (AGGREGATE_SPEC, INCIDENT_SPEC)
) -> ToolChoiceRequest:
    return ToolChoiceRequest(instructions=INSTRUCTIONS, question=question, tools=tools)


def _function_call(name: str, arguments: str) -> dict[str, JsonValue]:
    return {"type": "function_call", "call_id": f"call_{name}", "name": name, "arguments": arguments}


def _plan_reply(output: list[JsonValue], model: str = CHAT_MODEL, status: str = "completed") -> ScriptedReply:
    return ScriptedReply(
        json_body={
            "id": "resp_fake",
            "object": "response",
            "status": status,
            "model": model,
            "output": output,
        }
    )


def _ok(model: str = CHAT_MODEL) -> ScriptedReply:
    return _plan_reply([_function_call("aggregate_failures", '{"lookback_hours": 6}')], model=model)


def _error(status: int) -> ScriptedReply:
    return ScriptedReply(status=status, json_body={"error": {"message": BODY_MARKER}})


def _choose_expecting_failure(
    adapter: OpenAiToolCallingLlm, request: ToolChoiceRequest | None = None
) -> LlmUnavailableError:
    with pytest.raises(LlmUnavailableError) as exc:
        adapter.choose_tools(request or _request())
    return exc.value


def _sent_input(server: FakeOpenAiServer) -> str:
    return server.requests[0].string_field("input")


# --- port ---


def test_openai_tool_calling_llm_is_assignable_to_the_tool_calling_port() -> None:
    # Arrange
    adapter: ToolCallingLlm = _adapter(closed_port_base_url())

    # Act
    choose = adapter.choose_tools

    # Assert
    assert choose.__name__ == "choose_tools"


# --- request ---


def test_choose_tools_posts_once_to_responses_with_bearer_key_and_json_content_type(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    adapter.choose_tools(_request())

    # Assert
    recorded = server.requests
    assert len(recorded) == 1
    assert recorded[0].path == "/v1/responses"
    assert recorded[0].headers["authorization"] == f"Bearer {CANARY_KEY}"
    assert recorded[0].headers["content-type"].split(";")[0].strip() == "application/json"


def test_choose_tools_sends_exactly_the_documented_body_keys(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request())

    # Assert
    assert set(server.requests[0].json_object()) == EXPECTED_BODY_KEYS


def test_choose_tools_sends_the_documented_constant_values(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request())

    # Assert
    body = server.requests[0].json_object()
    assert body["model"] == CHAT_MODEL
    assert body["tool_choice"] == "required"
    assert body["max_output_tokens"] == 600
    assert body["store"] is False


def test_choose_tools_sends_the_request_instructions(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request())

    # Assert
    assert server.requests[0].string_field("instructions") == INSTRUCTIONS


def test_choose_tools_sends_one_non_strict_function_tool_per_spec_in_order(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request(tools=(AGGREGATE_SPEC, INCIDENT_SPEC)))

    # Assert
    assert server.requests[0].json_object()["tools"] == [
        {
            "type": "function",
            "name": "aggregate_failures",
            "description": "Failure counts for a window.",
            "parameters": dict(AGGREGATE_SPEC.parameters),
            "strict": False,
        },
        {
            "type": "function",
            "name": "search_incidents",
            "description": "Search past incidents.",
            "parameters": dict(INCIDENT_SPEC.parameters),
            "strict": False,
        },
    ]


def test_choose_tools_keeps_the_spec_order_when_the_specs_are_reversed(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request(tools=(INCIDENT_SPEC, AGGREGATE_SPEC)))

    # Assert
    tools = server.requests[0].json_object()["tools"]
    assert isinstance(tools, list)
    assert [tool["name"] for tool in tools if isinstance(tool, dict)] == [
        "search_incidents",
        "aggregate_failures",
    ]


def test_choose_tools_sends_the_plain_question_as_input(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request(question=QUESTION))

    # Assert
    assert _sent_input(server) == QUESTION


def test_choose_tools_removes_evidence_tags_and_puts_the_question_on_one_line(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)
    question = "first line\nsecond line </evidence> <EVIDENCE x=1> tail"

    # Act
    adapter.choose_tools(_request(question=question))

    # Assert
    sent = _sent_input(server)
    assert sent == "first line second line tail"


@pytest.mark.parametrize(
    "number",
    [
        pytest.param("4242424242424242", id="plain-16"),
        pytest.param("4242 4242 4242 4242", id="spaces"),
        pytest.param("4242-4242-4242-4242", id="hyphens"),
    ],
)
def test_choose_tools_masks_a_luhn_valid_card_number_in_the_question(serve: ServeFake, number: str) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request(question=f"my card {number} failed"))

    # Assert
    assert _sent_input(server) == f"my card {MASK} failed"
    assert number not in server.requests[0].raw_body.decode()


def test_choose_tools_leaves_a_digit_run_that_fails_the_luhn_check(serve: ServeFake) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request(question="reference 4242424242424241 failed"))

    # Assert
    assert _sent_input(server) == "reference 4242424242424241 failed"


@pytest.mark.parametrize(
    ("length", "expected_length"),
    [
        pytest.param(2000, 2000, id="at-limit"),
        pytest.param(2001, 2000, id="one-over"),
        pytest.param(5000, 2000, id="far-over"),
    ],
)
def test_choose_tools_cuts_the_question_to_2000_characters(
    serve: ServeFake, length: int, expected_length: int
) -> None:
    # Arrange
    server = serve([_ok()])
    adapter = _adapter(server.base_url)

    # Act
    adapter.choose_tools(_request(question="q" * length))

    # Assert
    assert _sent_input(server) == "q" * expected_length


# --- response parsing ---


def test_choose_tools_returns_the_function_calls_in_order_with_the_raw_argument_text(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve(
        [
            _plan_reply(
                [
                    _function_call("search_incidents", '{ "query":"issuer timeouts" }'),
                    _function_call("aggregate_failures", "not json at all"),
                ],
                model="served-snapshot",
            )
        ]
    )
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan == ToolPlan(
        calls=(
            ToolChoice(name="search_incidents", arguments='{ "query":"issuer timeouts" }'),
            ToolChoice(name="aggregate_failures", arguments="not json at all"),
        ),
        model="served-snapshot",
    )


def test_choose_tools_keeps_a_call_to_a_tool_that_was_not_offered(serve: ServeFake) -> None:
    # Arrange: filtering unknown names is the registry's job, not the adapter's
    server = serve([_plan_reply([_function_call("drop_everything", "{}")])])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan.calls == (ToolChoice(name="drop_everything", arguments="{}"),)


def test_choose_tools_returns_at_most_eight_calls_keeping_the_first_eight(serve: ServeFake) -> None:
    # Arrange
    output: list[JsonValue] = [_function_call(f"tool_{index}", "{}") for index in range(10)]
    server = serve([_plan_reply(output)])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert [call.name for call in plan.calls] == [f"tool_{index}" for index in range(8)]


def test_choose_tools_returns_exactly_eight_calls_when_the_model_sent_eight(serve: ServeFake) -> None:
    # Arrange
    output: list[JsonValue] = [_function_call(f"tool_{index}", "{}") for index in range(8)]
    server = serve([_plan_reply(output)])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert len(plan.calls) == 8


def test_choose_tools_ignores_output_items_that_are_not_function_calls(serve: ServeFake) -> None:
    # Arrange
    output: list[JsonValue] = [
        {"type": "reasoning", "summary": []},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I will help"}]},
        _function_call("aggregate_failures", "{}"),
        {"type": "web_search_call", "name": "aggregate_failures", "arguments": "{}"},
        "a bare string",
        42,
        _function_call("search_incidents", '{"query": "x"}'),
    ]
    server = serve([_plan_reply(output)])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan.calls == (
        ToolChoice(name="aggregate_failures", arguments="{}"),
        ToolChoice(name="search_incidents", arguments='{"query": "x"}'),
    )


@pytest.mark.parametrize(
    "item",
    [
        pytest.param({"type": "function_call", "arguments": "{}"}, id="name-missing"),
        pytest.param({"type": "function_call", "name": 7, "arguments": "{}"}, id="name-not-a-string"),
        pytest.param({"type": "function_call", "name": "aggregate_failures"}, id="arguments-missing"),
        pytest.param(
            {"type": "function_call", "name": "aggregate_failures", "arguments": {"lookback_hours": 6}},
            id="arguments-already-an-object",
        ),
        pytest.param({"type": "function_call", "name": None, "arguments": None}, id="both-null"),
    ],
)
def test_choose_tools_skips_a_function_call_without_a_string_name_and_string_arguments(
    serve: ServeFake, item: dict[str, JsonValue]
) -> None:
    # Arrange
    server = serve([_plan_reply([item, _function_call("search_incidents", '{"query": "x"}')])])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan.calls == (ToolChoice(name="search_incidents", arguments='{"query": "x"}'),)


def test_choose_tools_returns_an_empty_plan_when_the_output_has_no_function_call(serve: ServeFake) -> None:
    # Arrange
    server = serve([completed_reply("I cannot decide", CHAT_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan == ToolPlan(calls=(), model=CHAT_MODEL)
    assert len(server.requests) == 1


def test_choose_tools_returns_an_empty_plan_when_the_output_list_is_empty(serve: ServeFake) -> None:
    # Arrange
    server = serve([_plan_reply([])])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan == ToolPlan(calls=(), model=CHAT_MODEL)


@pytest.mark.parametrize(
    "model_field",
    [
        pytest.param({}, id="absent"),
        pytest.param({"model": 123}, id="not-a-string"),
        pytest.param({"model": "bad model name!"}, id="invalid-characters"),
    ],
)
def test_choose_tools_reports_the_configured_model_when_the_response_model_is_missing_or_invalid(
    serve: ServeFake, model_field: dict[str, JsonValue]
) -> None:
    # Arrange
    body: dict[str, JsonValue] = {
        "status": "completed",
        "output": [_function_call("aggregate_failures", "{}")],
        **model_field,
    }
    server = serve([ScriptedReply(json_body=body)])
    adapter = _adapter(server.base_url)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan.model == CHAT_MODEL


# --- failures: every one raises LlmUnavailableError ---


@dataclass(frozen=True)
class FailureCase:
    reply: ScriptedReply
    budget_seconds: float = 5.0


NOT_COMPLETED_STATUSES = ("incomplete", "failed", "in_progress", "cancelled")
FAILURE_CASES = [
    pytest.param(FailureCase(_error(400)), id="http-400"),
    pytest.param(FailureCase(_error(401)), id="http-401"),
    pytest.param(FailureCase(_error(403)), id="http-403"),
    pytest.param(FailureCase(_error(422)), id="http-422"),
    pytest.param(FailureCase(ScriptedReply(raw_body=f"<html>{BODY_MARKER}</html>".encode())), id="not-json"),
    pytest.param(FailureCase(ScriptedReply(raw_body=b"")), id="empty-body"),
    pytest.param(FailureCase(ScriptedReply(json_body=[BODY_MARKER])), id="json-array"),
    pytest.param(FailureCase(ScriptedReply(json_body=BODY_MARKER)), id="json-string"),
    pytest.param(
        FailureCase(ScriptedReply(json_body={"output": [_function_call("aggregate_failures", "{}")]})),
        id="status-missing",
    ),
    *[
        pytest.param(
            FailureCase(
                _plan_reply([_function_call("aggregate_failures", BODY_MARKER)], status=status),
            ),
            id=f"status-{status}",
        )
        for status in NOT_COMPLETED_STATUSES
    ],
    pytest.param(
        FailureCase(ScriptedReply(delay_seconds=2.0, json_body={}), budget_seconds=0.5), id="timeout"
    ),
]


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_choose_tools_raises_llm_unavailable_after_one_request_when_the_failure_is_not_retryable(
    serve: ServeFake, case: FailureCase
) -> None:
    # Arrange
    server = serve([case.reply, _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL, budget_seconds=case.budget_seconds)

    # Act
    error = _choose_expecting_failure(adapter)

    # Assert
    assert isinstance(error, LlmUnavailableError)
    assert len(server.requests) == 1


@pytest.mark.parametrize("case", FAILURE_CASES)
def test_choose_tools_error_has_no_cause_or_context_and_leaks_no_key_question_or_body(
    serve: ServeFake, case: FailureCase
) -> None:
    # Arrange
    server = serve([case.reply])
    adapter = _adapter(server.base_url, budget_seconds=case.budget_seconds)

    # Act
    error = _choose_expecting_failure(adapter, _request(question=QUESTION_MARKER))

    # Assert
    message = str(error)
    for secret in (CANARY_KEY, BODY_MARKER, QUESTION_MARKER):
        assert secret not in message
    assert error.__cause__ is None
    assert error.__context__ is None


def test_choose_tools_raises_llm_unavailable_without_cause_when_the_connection_is_refused() -> None:
    # Arrange
    adapter = _adapter(closed_port_base_url(), fallback_model=None)

    # Act
    error = _choose_expecting_failure(adapter, _request(question=QUESTION_MARKER))

    # Assert
    assert CANARY_KEY not in str(error)
    assert QUESTION_MARKER not in str(error)
    assert error.__cause__ is None
    assert error.__context__ is None


def test_choose_tools_error_message_is_short_and_fixed_per_failure_kind(serve: ServeFake) -> None:
    # Arrange
    server = serve([_error(401)])
    adapter = _adapter(server.base_url)

    # Act
    error = _choose_expecting_failure(adapter)

    # Assert
    assert len(str(error)) <= 40


# --- fallback ---


@pytest.mark.parametrize("status", [404, 408, 409, 429, 500, 502, 503, 504])
def test_choose_tools_falls_back_on_retryable_http_statuses(serve: ServeFake, status: int) -> None:
    # Arrange
    server = serve([_error(status), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan == ToolPlan(
        calls=(ToolChoice(name="aggregate_failures", arguments='{"lookback_hours": 6}'),),
        model=FALLBACK_MODEL,
    )
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


def test_choose_tools_sends_the_same_tools_and_input_to_the_fallback_model(serve: ServeFake) -> None:
    # Arrange
    server = serve([_error(429), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    adapter.choose_tools(_request())

    # Assert
    first, second = (request.json_object() for request in server.requests)
    assert {key: value for key, value in first.items() if key != "model"} == {
        key: value for key, value in second.items() if key != "model"
    }
    assert [request.headers["authorization"] for request in server.requests] == [f"Bearer {CANARY_KEY}"] * 2


def test_choose_tools_falls_back_when_the_connection_drops_without_a_response(serve: ServeFake) -> None:
    # Arrange
    server = serve([ScriptedReply(drop_connection=True), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    plan = adapter.choose_tools(_request())

    # Assert
    assert plan.model == FALLBACK_MODEL
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_choose_tools_makes_one_request_and_no_fallback_on_non_retryable_statuses(
    serve: ServeFake, status: int
) -> None:
    # Arrange
    server = serve([_error(status), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    _choose_expecting_failure(adapter)

    # Assert
    assert server.models() == [CHAT_MODEL]


def test_choose_tools_makes_one_request_on_a_503_when_no_fallback_model_is_set(serve: ServeFake) -> None:
    # Arrange
    server = serve([_error(503), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=None)

    # Act
    _choose_expecting_failure(adapter)

    # Assert
    assert server.models() == [CHAT_MODEL]


def test_choose_tools_makes_one_request_on_a_503_when_the_fallback_equals_the_chat_model(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([_error(503), _ok()])
    adapter = _adapter(server.base_url, fallback_model=CHAT_MODEL)

    # Act
    _choose_expecting_failure(adapter)

    # Assert
    assert server.models() == [CHAT_MODEL]


def test_choose_tools_makes_two_requests_at_most_when_both_models_fail(serve: ServeFake) -> None:
    # Arrange
    server = serve([_error(503), _error(429), _ok()])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    error = _choose_expecting_failure(adapter)

    # Assert
    assert server.models() == [CHAT_MODEL, FALLBACK_MODEL]
    assert BODY_MARKER not in str(error)


def test_choose_tools_does_not_fall_back_on_an_incomplete_response(serve: ServeFake) -> None:
    # Arrange
    server = serve([_plan_reply([], status="incomplete"), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)

    # Act
    _choose_expecting_failure(adapter)

    # Assert
    assert server.models() == [CHAT_MODEL]


# --- budget ---


def test_choose_tools_raises_within_the_budget_and_makes_no_fallback_request_when_the_primary_hangs(
    serve: ServeFake,
) -> None:
    # Arrange
    server = serve([ScriptedReply(delay_seconds=2.0, json_body={}), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL, budget_seconds=0.5)
    started = time.monotonic()

    # Act
    _choose_expecting_failure(adapter)

    # Assert
    elapsed = time.monotonic() - started
    server.wait_for_request_count(1)
    time.sleep(0.3)
    assert elapsed < 1.5
    assert server.models() == [CHAT_MODEL]


# --- construction: key and base URL ---


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("", id="empty"),
        pytest.param(" sk-x", id="leading-space"),
        pytest.param("sk-x\n", id="trailing-newline"),
        pytest.param("sk-é", id="latin-accent"),
        pytest.param("sk-\tx", id="inner-tab"),
    ],
)
def test_openai_tool_calling_llm_rejects_an_api_key_that_is_empty_not_ascii_not_printable_or_not_stripped(
    key: str,
) -> None:
    # Arrange: the key is the parameter

    # Act
    with pytest.raises(ValueError) as exc:
        _adapter(closed_port_base_url(), api_key=key)

    # Assert
    assert str(exc.value) == KEY_ERROR


@pytest.mark.parametrize(
    "url",
    [
        pytest.param("http://api.example.com/v1", id="plain-http-remote"),
        pytest.param("https://user:pw-canary@api.example.com/v1", id="userinfo"),
        pytest.param("https://api.example.com/v1?key=abc-canary", id="query"),
        pytest.param("ftp://api.example.com/v1", id="other-scheme"),
    ],
)
def test_openai_tool_calling_llm_rejects_an_unsafe_base_url_with_a_fixed_message(url: str) -> None:
    # Arrange: the URL is the parameter

    # Act
    with pytest.raises(ValueError) as exc:
        _adapter(url)

    # Assert
    assert str(exc.value) == BASE_URL_ERROR
    assert "canary" not in str(exc.value)


def test_openai_tool_calling_llm_accepts_https_and_loopback_http_base_urls() -> None:
    # Arrange
    urls = ("https://api.example.com/v1", "http://127.0.0.1:8080/v1", "http://localhost:9/v1")

    # Act
    adapters = [_adapter(url) for url in urls]

    # Assert
    assert len(adapters) == 3


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
    adapter.choose_tools(_request())

    # Assert
    recorded = server.requests[0]
    header_hits = {name for name, value in recorded.headers.items() if CANARY_KEY in value}
    assert header_hits == {"authorization"}
    assert CANARY_KEY not in recorded.path
    assert CANARY_KEY.encode() not in recorded.raw_body


def test_the_adapter_does_not_log_the_key_or_the_question(
    serve: ServeFake, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    server = serve([_error(503), _ok(model=FALLBACK_MODEL)])
    adapter = _adapter(server.base_url, fallback_model=FALLBACK_MODEL)
    failing = _adapter(closed_port_base_url())
    caplog.set_level(logging.DEBUG)

    # Act
    adapter.choose_tools(_request(question=QUESTION_MARKER))
    _choose_expecting_failure(failing, _request(question=QUESTION_MARKER))

    # Assert
    messages = [record.getMessage() for record in caplog.records]
    assert [record.name for record in caplog.records if record.name.startswith("ops_ai")] == []
    assert [message for message in messages if CANARY_KEY in message or QUESTION_MARKER in message] == []
