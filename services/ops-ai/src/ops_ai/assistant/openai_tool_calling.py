import json
import threading
import time

import httpx
from pydantic import SecretStr

from ops_ai.assistant.llm_port import LlmUnavailableError
from ops_ai.assistant.openai_llm import (
    _MODEL_NAME,
    API_KEY_ERROR,
    LLM_BUDGET_SECONDS,
    MAX_CONCURRENT_CALLS,
    MAX_OUTPUT_TOKENS,
    MAX_QUESTION_CHARS,
    MAX_RESPONSE_BYTES,
    OPENAI_BASE_URL,
    RETRYABLE_STATUSES,
    _Failure,
    _one_line,
    validate_base_url,
)
from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan


class OpenAiToolCallingLlm:
    """Choose function calls through the Responses API with one fallback and a shared time budget.

    Errors carry only a short reason and are raised `from None`, so no traceback holds the request headers.
    """

    def __init__(
        self,
        api_key: SecretStr,
        chat_model: str,
        fallback_model: str | None,
        base_url: str = OPENAI_BASE_URL,
        budget_seconds: float = LLM_BUDGET_SECONDS,
        max_concurrent: int = MAX_CONCURRENT_CALLS,
    ) -> None:
        raw = api_key.get_secret_value()
        if not raw or not raw.isascii() or not raw.isprintable() or raw != raw.strip():
            raise ValueError(API_KEY_ERROR)
        self._api_key = api_key
        self._chat_model = chat_model
        self._fallback_model = fallback_model
        self._url = f"{validate_base_url(base_url).rstrip('/')}/responses"
        self._budget_seconds = budget_seconds
        self._slots = threading.BoundedSemaphore(max_concurrent)

    def __repr__(self) -> str:
        return (
            f"OpenAiToolCallingLlm(chat_model={self._chat_model!r}, fallback_model={self._fallback_model!r})"
        )

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        if not self._slots.acquire(blocking=False):
            raise LlmUnavailableError("openai: busy")
        try:
            result = self._complete(request)
        finally:
            self._slots.release()
        if isinstance(result, ToolPlan):
            return result
        raise LlmUnavailableError(f"openai: {result.reason}")

    def _complete(self, request: ToolChoiceRequest) -> ToolPlan | _Failure:
        deadline = time.monotonic() + self._budget_seconds
        rendered = _one_line(request.question, MAX_QUESTION_CHARS, mask=True)
        result = self._attempt(self._chat_model, rendered, request, deadline)
        if isinstance(result, ToolPlan):
            return result
        fallback = self._fallback_model
        if result.retryable and fallback and fallback != self._chat_model and time.monotonic() < deadline:
            return self._attempt(fallback, rendered, request, deadline)
        return result

    def _attempt(
        self, model: str, rendered: str, request: ToolChoiceRequest, deadline: float
    ) -> ToolPlan | _Failure:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return _Failure("timeout", retryable=False)
        payload = {
            "model": model,
            "instructions": request.instructions,
            "tools": [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": dict(tool.parameters),
                    "strict": False,
                }
                for tool in request.tools
            ],
            "tool_choice": "required",
            "input": rendered,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "store": False,
        }
        headers = {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}
        body = bytearray()
        try:
            with (
                httpx.Client(timeout=httpx.Timeout(remaining), trust_env=False) as client,
                client.stream("POST", self._url, json=payload, headers=headers) as response,
            ):
                status = response.status_code
                if status != 200:
                    return _Failure(f"http {status}", retryable=status in RETRYABLE_STATUSES or status >= 500)
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        return _Failure("response too large", retryable=False)
                    if time.monotonic() >= deadline:
                        return _Failure("timeout", retryable=True)
        except httpx.TimeoutException:
            return _Failure("timeout", retryable=True)
        except httpx.HTTPError:
            return _Failure("connection error", retryable=True)
        except Exception:
            return _Failure("client error", retryable=False)
        try:
            return self._parse(bytes(body), model)
        except Exception:
            return _Failure("invalid body", retryable=False)

    @staticmethod
    def _parse(content: bytes, model: str) -> ToolPlan | _Failure:
        try:
            body: object = json.loads(content)
        except (ValueError, RecursionError):
            return _Failure("invalid json", retryable=False)
        if not isinstance(body, dict):
            return _Failure("invalid body", retryable=False)
        if body.get("status") != "completed":
            return _Failure("incomplete response", retryable=False)
        output = body.get("output")
        if not isinstance(output, list):
            return _Failure("invalid body", retryable=False)
        calls: list[ToolChoice] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "function_call":
                continue
            name, arguments = item.get("name"), item.get("arguments")
            if isinstance(name, str) and isinstance(arguments, str):
                calls.append(ToolChoice(name=name, arguments=arguments))
            if len(calls) == 8:
                break
        reported = body.get("model")
        served = reported if isinstance(reported, str) and _MODEL_NAME.fullmatch(reported) else model
        return ToolPlan(calls=tuple(calls), model=served)
