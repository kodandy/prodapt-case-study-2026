from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import ABSTAIN_TEXT, Claim, ground_claims, grounded_narration
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import AnswerMode
from ops_ai.assistant.tool_calling import ToolCallingLlm, ToolChoiceRequest
from ops_ai.investigation.service import ToolCall
from ops_ai.tools.planner import RuleBasedToolPlanner
from ops_ai.tools.registry import ToolExecutionError, ToolRegistry, ToolRejectedError

MAX_TOOL_CALLS = 4
MAX_REJECTED_NAME_CHARS = 64


@dataclass(frozen=True)
class RejectedCall:
    name: str
    reason: Literal["unknown_tool", "invalid_args", "limit_exceeded", "execution_failed"]


@dataclass(frozen=True)
class ToolAnswer:
    claims: tuple[Claim, ...]
    abstained: bool
    tool_calls: tuple[ToolCall, ...]
    rejected_calls: tuple[RejectedCall, ...]
    evidence: tuple[EvidenceItem, ...]
    mode: AnswerMode
    text: str | None
    model: str | None
    planner_model: str


def answer_with_tools[S](
    session_factory: sessionmaker[Session],
    scope: S,
    question: str,
    now: datetime,
    registry: ToolRegistry[S],
    planner: ToolCallingLlm,
    llm: LlmPort,
) -> ToolAnswer:
    request = ToolChoiceRequest(
        instructions="Select only offered tools to answer the question. Use the supplied argument schemas.",
        question=question[:2000],
        tools=registry.specs(),
    )
    try:
        plan = planner.choose_tools(request)
    except LlmUnavailableError:
        plan = RuleBasedToolPlanner().choose_tools(request)
    calls: list[ToolCall] = []
    rejected: list[RejectedCall] = []
    evidence: list[EvidenceItem] = []
    collected: list[Claim] = []
    for index, call in enumerate(plan.calls):
        if index >= MAX_TOOL_CALLS:
            rejected.append(RejectedCall(call.name[:MAX_REJECTED_NAME_CHARS], "limit_exceeded"))
            continue
        try:
            args = registry.validate(call.name, call.arguments)
            with session_factory() as session:
                output = registry.invoke(session, scope, now, call.name, call.arguments)
        except ToolRejectedError as error:
            rejected.append(RejectedCall(call.name[:MAX_REJECTED_NAME_CHARS], error.reason))
            continue
        except ToolExecutionError:
            rejected.append(RejectedCall(call.name[:MAX_REJECTED_NAME_CHARS], "execution_failed"))
            continue
        calls.append(
            ToolCall(
                call.name,
                {key: str(value) for key, value in args.model_dump(mode="json", exclude_none=True).items()},
            )
        )
        evidence.extend(output.evidence)
        collected.extend(output.claims)
    items = tuple(evidence)
    claims = ground_claims(collected, items)
    mode: AnswerMode = "t2_fallback"
    text: str | None = ABSTAIN_TEXT if not claims else None
    model: str | None = None
    if claims:
        try:
            narration = llm.complete(LlmRequest(question=question, evidence=items))
            text = grounded_narration(narration.text, items)
            if text is not None:
                mode, model = "llm", narration.model
        except LlmUnavailableError:
            pass
    return ToolAnswer(claims, not claims, tuple(calls), tuple(rejected), items, mode, text, model, plan.model)
