from dataclasses import dataclass
from typing import Protocol

from ops_ai.assistant.evidence import EvidenceItem


@dataclass(frozen=True)
class LlmRequest:
    question: str
    evidence: tuple[EvidenceItem, ...]
    require_citations: bool = False
    """R1-5 grounded /ask: every sentence must end with the [ref] of the evidence it uses."""


@dataclass(frozen=True)
class LlmResponse:
    text: str
    model: str


class LlmUnavailableError(Exception):
    """Any provider failure (outage, timeout, budget): the caller falls back to T2."""


class LlmPort(Protocol):
    def complete(self, request: LlmRequest) -> LlmResponse: ...


class MockLlm:
    """Rule-based narration over the evidence; proves wiring only, never quality."""

    model = "mock"

    def complete(self, request: LlmRequest) -> LlmResponse:
        if not request.evidence:
            return LlmResponse(text="No matching evidence was found for this question.", model=self.model)
        lines = [f"- {item.summary} [{item.ref}]" for item in request.evidence]
        text = f"Found {len(request.evidence)} evidence items for: {request.question}\n" + "\n".join(lines)
        return LlmResponse(text=text, model=self.model)
