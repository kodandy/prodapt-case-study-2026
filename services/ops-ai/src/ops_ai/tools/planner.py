import json
import re

from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan
from ops_ai.investigation.query import rules_failure_args

_UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")
_ORDER = (
    "get_payment_status",
    "analyze_fraud",
    "compare_baseline",
    "change_timeline",
    "failure_reasons",
    "search_incidents",
)


class RuleBasedToolPlanner:
    model = "rules"

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        question = request.question
        lower = question.lower()
        offered = {tool.name: tool for tool in request.tools}
        selected: dict[str, dict[str, object]] = {}
        status = offered.get("get_payment_status")
        if status is not None:
            properties = status.parameters.get("properties", {})
            if isinstance(properties, dict):
                match = _UUID.search(question)
                key = "payment_id" if "payment_id" in properties else "transfer_id"
                if match and key in properties:
                    selected[status.name] = {key: match.group(0)}
                elif not status.parameters.get("required") and any(
                    word in lower for word in ("status", "transfer")
                ):
                    selected[status.name] = {}
        if any(w in lower for w in ("fraud", "card testing", "micro")):
            selected["analyze_fraud"] = {}
        if "fail" in lower or "declin" in lower:
            selected["compare_baseline"] = {}
            selected["change_timeline"] = {}
            reason = offered.get("failure_reasons")
            if reason is not None:
                properties = reason.parameters.get("properties", {})
                if isinstance(properties, dict):
                    selected[reason.name] = {
                        key: value
                        for key, value in rules_failure_args(question).model_dump(mode="json").items()
                        if key in properties
                    }
        if any(w in lower for w in ("incident", "outage", "fix")):
            selected["search_incidents"] = {"query": question[:200]}
        return ToolPlan(
            calls=tuple(
                ToolChoice(name, json.dumps(selected[name]))
                for name in _ORDER
                if name in selected and name in offered
            ),
            model="rules",
        )
