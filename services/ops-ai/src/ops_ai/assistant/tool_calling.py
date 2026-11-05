from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: Mapping[str, object]
    """JSON Schema of the arguments object."""


@dataclass(frozen=True)
class ToolChoiceRequest:
    instructions: str
    question: str
    tools: tuple[ToolSpec, ...]


@dataclass(frozen=True)
class ToolChoice:
    name: str
    arguments: str
    """Raw JSON text from the model: untrusted until a registry validates it."""


@dataclass(frozen=True)
class ToolPlan:
    calls: tuple[ToolChoice, ...]
    model: str


class ToolCallingLlm(Protocol):
    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        """Raises LlmUnavailableError on any provider failure."""
        ...
