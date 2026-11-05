"""B4.1: a natural-language failure question becomes a typed FailureQuery. The merchant comes from scope."""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ops_ai.assistant.llm_port import LlmUnavailableError
from ops_ai.assistant.tool_calling import ToolCallingLlm, ToolChoiceRequest, ToolSpec
from ops_ai.scope import Scope

FailureMethod = Literal["card", "bank_transfer", "wallet", "upi"]
FailureStatus = Literal["declined", "failed"]
QuerySource = Literal["llm", "rules"]

SUBMIT_FAILURE_QUERY = "submit_failure_query"
MAX_WINDOW_HOURS = 720
MAX_QUESTION_CHARS = 2000

_INSTRUCTIONS = (
    "Turn the merchant's question about failed payments into filters by calling submit_failure_query once. "
    "Use relative hours only. Leave a filter out when the question does not name it."
)


class FailureQueryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lookback_hours: int = Field(default=24, ge=1, le=MAX_WINDOW_HOURS)
    end_hours_ago: int = Field(default=0, ge=0, le=MAX_WINDOW_HOURS)
    method: FailureMethod | None = None
    status: FailureStatus | None = None
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")

    @model_validator(mode="after")
    def _window_fits(self) -> Self:
        if self.lookback_hours + self.end_hours_ago > MAX_WINDOW_HOURS:
            raise ValueError("lookback_hours + end_hours_ago must not exceed 720")
        return self


@dataclass(frozen=True)
class FailureQuery:
    merchant_id: uuid.UUID
    start: datetime
    end: datetime
    method: FailureMethod | None
    status: FailureStatus | None
    country: str | None
    currency: str | None
    source: QuerySource
    unapplied_filters: tuple[str, ...]
    """Filters the data cannot apply: payment facts carry no country."""


_HOURS = re.compile(r"\b(?:last|past) (\d+) hours?\b")
_DAYS = re.compile(r"\b(?:last|past) (\d+) days?\b")
_PART_OF_DAY = re.compile(r"\bthis (?:afternoon|morning|evening)\b")
_CURRENCY = re.compile(r"\b(usd|eur|inr|jpy|gbp)\b")
_METHODS: tuple[tuple[re.Pattern[str], FailureMethod], ...] = (
    (re.compile(r"\bbank transfers?\b"), "bank_transfer"),
    (re.compile(r"\bwallets?\b"), "wallet"),
    (re.compile(r"\bupi\b"), "upi"),
    (re.compile(r"\bcards?\b"), "card"),
)
_COUNTRIES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bindia\b"), "IN"),
    (re.compile(r"\bjapan\b"), "JP"),
    (re.compile(r"\b(?:united states|usa)\b"), "US"),
    (re.compile(r"\bgermany\b"), "DE"),
    (re.compile(r"\bfrance\b"), "FR"),
    (re.compile(r"\b(?:united kingdom|uk)\b"), "GB"),
)


def _clamp(hours: int) -> int:
    return min(max(hours, 1), MAX_WINDOW_HOURS)


def _window(text: str) -> tuple[int, int]:
    if _PART_OF_DAY.search(text):
        return 6, 0
    if hours := _HOURS.search(text):
        return _clamp(int(hours.group(1))), 0
    if days := _DAYS.search(text):
        return _clamp(int(days.group(1)) * 24), 0
    if "yesterday" in text:
        return 24, 24
    if "this week" in text or "last week" in text:
        return 168, 0
    return 24, 0


def rules_failure_args(question: str) -> FailureQueryArgs:
    text = question.lower()
    lookback_hours, end_hours_ago = _window(text)
    method = next((value for pattern, value in _METHODS if pattern.search(text)), None)
    country = next((value for pattern, value in _COUNTRIES if pattern.search(text)), None)
    currency_match = _CURRENCY.search(text)
    status: FailureStatus | None = "declined" if "declin" in text and "fail" not in text else None
    return FailureQueryArgs(
        lookback_hours=lookback_hours,
        end_hours_ago=end_hours_ago,
        method=method,
        status=status,
        country=country,
        currency=currency_match.group(1).upper() if currency_match else None,
    )


def to_failure_query(
    args: FailureQueryArgs, scope: Scope, now: datetime, source: QuerySource
) -> FailureQuery:
    end = now - timedelta(hours=args.end_hours_ago)
    return FailureQuery(
        merchant_id=scope.merchant_id,
        start=end - timedelta(hours=args.lookback_hours),
        end=end,
        method=args.method,
        status=args.status,
        country=args.country,
        currency=args.currency,
        source=source,
        unapplied_filters=("country",) if args.country is not None else (),
    )


def submit_failure_query_spec() -> ToolSpec:
    return ToolSpec(
        name=SUBMIT_FAILURE_QUERY,
        description="Submit the failed-payment filters implied by the question.",
        parameters=FailureQueryArgs.model_json_schema(),
    )


def _llm_args(question: str, tool_llm: ToolCallingLlm) -> FailureQueryArgs | None:
    request = ToolChoiceRequest(
        instructions=_INSTRUCTIONS, question=question, tools=(submit_failure_query_spec(),)
    )
    try:
        plan = tool_llm.choose_tools(request)
    except LlmUnavailableError:
        return None
    if len(plan.calls) != 1 or plan.calls[0].name != SUBMIT_FAILURE_QUERY:
        return None
    try:
        payload: object = json.loads(plan.calls[0].arguments)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        return FailureQueryArgs.model_validate(payload)
    except ValidationError:
        return None


def parse_failure_query(
    question: str, scope: Scope, now: datetime, tool_llm: ToolCallingLlm | None
) -> FailureQuery:
    """LLM function calling first; any failure or schema-invalid answer falls back to the rules."""
    bounded = question[:MAX_QUESTION_CHARS]
    if tool_llm is not None and (args := _llm_args(bounded, tool_llm)) is not None:
        return to_failure_query(args, scope, now, "llm")
    return to_failure_query(rules_failure_args(bounded), scope, now, "rules")
