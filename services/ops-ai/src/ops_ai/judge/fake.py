"""Deterministic judge LLM for CI (docs/specs/evaluation-judge.md section 6.3)."""

from ops_ai.assistant.llm_port import LlmRequest, LlmResponse

RUBRIC_JSON_PREFIX = "RUBRIC_JSON:"


class FakeJudgeLlm:
    """Returns `reply` verbatim, or else the rubric's own opinion from the prompt's RUBRIC_JSON line."""

    model = "fake-judge"

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if self.reply is not None:
            return LlmResponse(text=self.reply, model=self.model)
        for line in request.question.splitlines():
            if line.startswith(RUBRIC_JSON_PREFIX):
                return LlmResponse(text=line.removeprefix(RUBRIC_JSON_PREFIX).strip(), model=self.model)
        return LlmResponse(text="{}", model=self.model)
