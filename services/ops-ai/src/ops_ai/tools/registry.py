import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import Claim
from ops_ai.assistant.tool_calling import ToolSpec
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


class ToolRejectedError(Exception):
    def __init__(self, reason: Literal["unknown_tool", "invalid_args"]) -> None:
        self.reason = reason
        super().__init__(reason)


class ToolExecutionError(Exception):
    def __init__(self, tool: str) -> None:
        self.tool = tool
        super().__init__("tool execution failed")


@dataclass(frozen=True)
class ToolOutput:
    evidence: tuple[EvidenceItem, ...]
    claims: tuple[Claim, ...]


@dataclass(frozen=True)
class ToolContext[S]:
    session: Session
    scope: S
    now: datetime


@dataclass(frozen=True)
class ToolDefinition[S]:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[[ToolContext[S], BaseModel], ToolOutput]


class ToolRegistry[S]:
    def __init__(self, tools: Sequence[ToolDefinition[S]]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("duplicate tool name")

    def names(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            ToolSpec(t.name, t.description, t.args_model.model_json_schema()) for t in self._tools.values()
        )

    def validate(self, name: str, arguments: str) -> BaseModel:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolRejectedError("unknown_tool")
        valid: BaseModel | None = None
        if len(arguments) <= 4096:
            try:
                payload: object = json.loads(arguments)
                if isinstance(payload, dict):
                    valid = tool.args_model.model_validate(payload)
            except (ValueError, RecursionError):
                pass
        if valid is None:
            raise ToolRejectedError("invalid_args")
        return valid

    def invoke(self, session: Session, scope: S, now: datetime, name: str, arguments: str) -> ToolOutput:
        args = self.validate(name, arguments)
        if session.in_transaction():
            raise ValueError("tool requires an idle session")
        failed = False
        output: ToolOutput | None = None
        try:
            session.execute(text("SET TRANSACTION READ ONLY"))
            output = self._tools[name].run(ToolContext(session, scope, now), args)
        except (SQLAlchemyError, ValueError):
            failed = True
        finally:
            session.rollback()
        if failed or output is None:
            raise ToolExecutionError(tool=name)
        return output
