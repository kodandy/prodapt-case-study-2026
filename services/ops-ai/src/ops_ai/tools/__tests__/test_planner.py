"""Tests for ops_ai/tools/planner.py, written from docs/specs/failure-tools.md section 9 (rule planner)."""

import json
import uuid

import pytest
from ops_ai.assistant.tool_calling import ToolCallingLlm, ToolChoiceRequest, ToolPlan, ToolSpec
from ops_ai.investigation.query import FailureQueryArgs
from ops_ai.tools.catalog import merchant_tools, user_tools
from ops_ai.tools.planner import RuleBasedToolPlanner

PAYMENT_ID = uuid.UUID("3f2b8c1e-5a4d-4e6f-9b0a-1c2d3e4f5a6b")
INSTRUCTIONS = "Pick tools."
MERCHANT_SPECS = merchant_tools().specs()
USER_SPECS = user_tools().specs()
FULL_ORDER = [
    "get_payment_status",
    "analyze_fraud",
    "compare_baseline",
    "change_timeline",
    "failure_reasons",
    "search_incidents",
]


def _plan(question: str, tools: tuple[ToolSpec, ...] = MERCHANT_SPECS) -> ToolPlan:
    request = ToolChoiceRequest(instructions=INSTRUCTIONS, question=question, tools=tools)
    return RuleBasedToolPlanner().choose_tools(request)


def _names(plan: ToolPlan) -> list[str]:
    return [call.name for call in plan.calls]


def _arguments(plan: ToolPlan, name: str) -> dict[str, object]:
    (call,) = [call for call in plan.calls if call.name == name]
    parsed: object = json.loads(call.arguments)
    assert isinstance(parsed, dict)
    return parsed


def _spec(name: str, properties: dict[str, object], required: tuple[str, ...] = ()) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} description",
        parameters={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    )


# --- port -------------------------------------------------------------------------------------------------


def test_rule_based_tool_planner_satisfies_the_tool_calling_port() -> None:
    # Arrange
    planner: ToolCallingLlm = RuleBasedToolPlanner()

    # Act
    plan = planner.choose_tools(ToolChoiceRequest(instructions=INSTRUCTIONS, question="hello", tools=()))

    # Assert
    assert plan == ToolPlan(calls=(), model="rules")


def test_plan_model_is_rules_whatever_the_question() -> None:
    # Arrange
    questions = ("hello", "why are card payments failing?", f"status of {PAYMENT_ID}")

    # Act
    models = [_plan(question).model for question in questions]

    # Assert
    assert models == ["rules", "rules", "rules"]


# --- picks per rule ---------------------------------------------------------------------------------------


def test_a_question_without_a_trigger_word_picks_nothing() -> None:
    # Arrange
    question = "hello there"

    # Act
    plan = _plan(question)

    # Assert
    assert plan.calls == ()


@pytest.mark.parametrize("word", ["fail", "failing", "failed", "declined", "declines", "declining"])
def test_a_failure_word_picks_compare_baseline_change_timeline_and_failure_reasons(word: str) -> None:
    # Arrange
    question = f"why are my card payments {word}?"

    # Act
    plan = _plan(question)

    # Assert
    assert _names(plan) == ["compare_baseline", "change_timeline", "failure_reasons"]


def test_compare_baseline_and_change_timeline_get_empty_argument_objects() -> None:
    # Arrange
    question = "why are payments failing?"

    # Act
    plan = _plan(question)

    # Assert
    assert _arguments(plan, "compare_baseline") == {}
    assert _arguments(plan, "change_timeline") == {}


def test_failure_reasons_arguments_are_the_rule_parsed_question_filtered_to_the_offered_keys() -> None:
    # Arrange
    question = "why are card payments failing in the last 3 hours?"

    # Act
    plan = _plan(question)

    # Assert
    (call,) = [call for call in plan.calls if call.name == "failure_reasons"]
    parsed = FailureQueryArgs.model_validate_json(call.arguments)
    assert parsed == FailureQueryArgs(lookback_hours=3, method="card")


def test_failure_reasons_arguments_only_hold_keys_the_offered_schema_has() -> None:
    # Arrange
    narrow = _spec("failure_reasons", {"lookback_hours": {"type": "integer"}})

    # Act
    plan = _plan("why are card payments failing in the last 3 hours?", (narrow,))

    # Assert
    assert _arguments(plan, "failure_reasons") == {"lookback_hours": 3}


def test_failure_reasons_arguments_for_the_user_schema_carry_only_the_lookback() -> None:
    # Arrange
    question = "why did my payments fail in the last 3 days?"

    # Act
    plan = _plan(question, USER_SPECS)

    # Assert
    assert _arguments(plan, "failure_reasons") == {"lookback_hours": 72}


@pytest.mark.parametrize("word", ["fraud", "card testing", "micro"])
def test_a_fraud_word_picks_analyze_fraud_with_empty_arguments(word: str) -> None:
    # Arrange
    question = f"is this {word} activity?"

    # Act
    plan = _plan(question)

    # Assert
    assert _names(plan) == ["analyze_fraud"]
    assert _arguments(plan, "analyze_fraud") == {}


@pytest.mark.parametrize("word", ["incident", "outage", "fix"])
def test_an_incident_word_picks_search_incidents_with_the_question_as_the_query(word: str) -> None:
    # Arrange
    question = f"was there a {word} like this before?"

    # Act
    plan = _plan(question)

    # Assert
    assert _names(plan) == ["search_incidents"]
    assert _arguments(plan, "search_incidents") == {"query": question}


def test_search_incidents_query_is_cut_to_the_first_200_characters() -> None:
    # Arrange
    question = "outage " + "x" * 400

    # Act
    plan = _plan(question)

    # Assert
    assert _arguments(plan, "search_incidents") == {"query": question[:200]}


def test_search_incidents_query_of_exactly_200_characters_is_kept_whole() -> None:
    # Arrange
    question = ("outage " + "y" * 193)[:200]
    assert len(question) == 200

    # Act
    plan = _plan(question)

    # Assert
    assert _arguments(plan, "search_incidents") == {"query": question}


# --- get_payment_status -----------------------------------------------------------------------------------


def test_a_uuid_in_the_question_picks_get_payment_status_with_payment_id_for_the_merchant_schema() -> None:
    # Arrange
    question = f"what happened to {PAYMENT_ID}?"

    # Act
    plan = _plan(question, MERCHANT_SPECS)

    # Assert
    assert _names(plan) == ["get_payment_status"]
    assert _arguments(plan, "get_payment_status") == {"payment_id": str(PAYMENT_ID)}


def test_a_uuid_in_the_question_picks_get_payment_status_with_transfer_id_for_the_user_schema() -> None:
    # Arrange
    question = f"what happened to {PAYMENT_ID}?"

    # Act
    plan = _plan(question, USER_SPECS)

    # Assert
    assert _names(plan) == ["get_payment_status"]
    assert _arguments(plan, "get_payment_status") == {"transfer_id": str(PAYMENT_ID)}


@pytest.mark.parametrize("word", ["status", "transfer"])
def test_status_or_transfer_picks_get_payment_status_for_a_schema_without_a_required_id(word: str) -> None:
    # Arrange
    question = f"what is the {word} of my last one?"

    # Act
    plan = _plan(question, USER_SPECS)

    # Assert
    assert _names(plan) == ["get_payment_status"]
    assert _arguments(plan, "get_payment_status") == {}


def test_status_without_a_uuid_does_not_pick_get_payment_status_for_a_schema_that_requires_an_id() -> None:
    # Arrange
    question = "what is the status of my last payment?"

    # Act
    plan = _plan(question, MERCHANT_SPECS)

    # Assert
    assert plan.calls == ()


def test_a_uuid_in_the_middle_of_a_long_sentence_is_found() -> None:
    # Arrange
    question = f"Hello, I noticed that order {PAYMENT_ID} was odd and I would like to know more please"

    # Act
    plan = _plan(question, MERCHANT_SPECS)

    # Assert
    assert _arguments(plan, "get_payment_status") == {"payment_id": str(PAYMENT_ID)}


# --- only offered tools, each once, fixed order -----------------------------------------------------------


def test_a_question_that_matches_every_rule_returns_each_offered_tool_once_in_the_fixed_order() -> None:
    # Arrange
    question = f"{PAYMENT_ID}: is this fraud? payments are failing, was there an incident?"

    # Act
    plan = _plan(question, MERCHANT_SPECS)

    # Assert
    assert _names(plan) == FULL_ORDER


def test_the_fixed_order_does_not_follow_the_order_of_the_offered_tools() -> None:
    # Arrange
    question = f"{PAYMENT_ID}: is this fraud? payments are failing, was there an incident?"
    reversed_specs = tuple(reversed(MERCHANT_SPECS))

    # Act
    plan = _plan(question, reversed_specs)

    # Assert
    assert _names(plan) == FULL_ORDER


def test_a_word_that_matches_two_rules_still_picks_each_tool_once() -> None:
    # Arrange
    question = "fail fail failed declined declining fix fix outage incident fraud fraud"

    # Act
    plan = _plan(question)

    # Assert
    names = _names(plan)
    assert names == [
        "analyze_fraud",
        "compare_baseline",
        "change_timeline",
        "failure_reasons",
        "search_incidents",
    ]
    assert len(names) == len(set(names))


def test_only_offered_tools_are_picked() -> None:
    # Arrange
    question = f"{PAYMENT_ID}: is this fraud? payments are failing, was there an incident?"
    offered = tuple(spec for spec in MERCHANT_SPECS if spec.name in {"search_incidents", "analyze_fraud"})

    # Act
    plan = _plan(question, offered)

    # Assert
    assert _names(plan) == ["analyze_fraud", "search_incidents"]


def test_no_tools_offered_gives_an_empty_plan() -> None:
    # Arrange
    question = f"{PAYMENT_ID}: is this fraud? payments are failing, was there an incident?"

    # Act
    plan = _plan(question, ())

    # Assert
    assert plan == ToolPlan(calls=(), model="rules")


def test_the_user_registry_never_gets_a_merchant_only_tool() -> None:
    # Arrange
    question = f"{PAYMENT_ID}: is this fraud? my transfers failed, was there an incident?"

    # Act
    plan = _plan(question, USER_SPECS)

    # Assert
    assert _names(plan) == ["get_payment_status", "failure_reasons"]


def test_picked_arguments_validate_against_the_real_merchant_registry() -> None:
    # Arrange
    question = f"{PAYMENT_ID}: is this fraud? card payments are failing in the last 2 hours, any incident?"
    registry = merchant_tools()

    # Act
    plan = _plan(question, MERCHANT_SPECS)

    # Assert
    validated = [registry.validate(call.name, call.arguments) for call in plan.calls]
    assert len(validated) == 6
    assert registry.validate("failure_reasons", plan.calls[4].arguments).model_dump()["lookback_hours"] == 2


def test_picked_arguments_validate_against_the_real_user_registry() -> None:
    # Arrange
    question = f"why did my transfers fail? {PAYMENT_ID}"
    registry = user_tools()

    # Act
    plan = _plan(question, USER_SPECS)

    # Assert
    validated = [registry.validate(call.name, call.arguments).model_dump() for call in plan.calls]
    assert validated == [{"transfer_id": PAYMENT_ID}, {"lookback_hours": 24}]
