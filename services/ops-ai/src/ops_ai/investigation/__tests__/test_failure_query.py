"""Tests for ops_ai/investigation/query.py, written from docs/specs/failure-tools.md sections 1, 4 (B4.1)."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.llm_port import LlmUnavailableError
from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan
from ops_ai.investigation.query import (
    SUBMIT_FAILURE_QUERY,
    FailureQuery,
    FailureQueryArgs,
    parse_failure_query,
    rules_failure_args,
    to_failure_query,
)
from ops_ai.scope import Scope

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
QUESTION = "why did card payments fail in the last 3 hours"


@dataclass
class FakeToolLlm:
    """Records every request; returns `plan`, or raises LlmUnavailableError when `unavailable`."""

    plan: ToolPlan = field(default_factory=lambda: ToolPlan(calls=(), model="fake"))
    unavailable: bool = False
    requests: list[ToolChoiceRequest] = field(default_factory=list)

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        self.requests.append(request)
        if self.unavailable:
            raise LlmUnavailableError("provider down")
        return self.plan


def _plan(*calls: ToolChoice) -> ToolPlan:
    return ToolPlan(calls=calls, model="fake")


def _submit(arguments: str) -> ToolChoice:
    return ToolChoice(name=SUBMIT_FAILURE_QUERY, arguments=arguments)


# --- rules_failure_args: window --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "lookback_hours", "end_hours_ago"),
    [
        ("why are payments failing this afternoon", 6, 0),
        ("why are payments failing this morning", 6, 0),
        ("why are payments failing this evening", 6, 0),
        ("what failed in the last 3 hours", 3, 0),
        ("what failed in the past 12 hours", 12, 0),
        ("what failed in the last 1 hour", 1, 0),
        ("what failed in the past 1 hour", 1, 0),
        ("what failed in the last 2 days", 48, 0),
        ("what failed in the past 3 days", 72, 0),
        ("what failed in the last 1 day", 24, 0),
        ("what failed in the past 1 day", 24, 0),
        ("what failed yesterday", 24, 24),
        ("what failed this week", 168, 0),
        ("what failed last week", 168, 0),
        ("what failed today", 24, 0),
        ("what failed", 24, 0),
    ],
)
def test_rules_failure_args_maps_each_window_phrase_to_lookback_and_offset(
    question: str, lookback_hours: int, end_hours_ago: int
) -> None:
    # Arrange / Act
    args = rules_failure_args(question)

    # Assert
    assert (args.lookback_hours, args.end_hours_ago) == (lookback_hours, end_hours_ago)


def test_rules_failure_args_is_case_insensitive_for_window_phrases() -> None:
    # Arrange / Act
    args = rules_failure_args("What FAILED in the LAST 5 Hours?")

    # Assert
    assert (args.lookback_hours, args.end_hours_ago) == (5, 0)


def test_rules_failure_args_uses_the_first_matching_window_phrase_in_spec_order() -> None:
    # Arrange / Act
    args = rules_failure_args("what failed today, especially this afternoon")

    # Assert
    assert (args.lookback_hours, args.end_hours_ago) == (6, 0)


@pytest.mark.parametrize(
    ("question", "lookback_hours"),
    [
        ("what failed in the last 30 days", 720),
        ("what failed in the last 31 days", 720),
        ("what failed in the last 9999 days", 720),
        ("what failed in the last 720 hours", 720),
        ("what failed in the last 721 hours", 720),
        ("what failed in the past 100000 hours", 720),
    ],
)
def test_rules_failure_args_clamps_oversized_windows_to_720_hours(question: str, lookback_hours: int) -> None:
    # Arrange / Act
    args = rules_failure_args(question)

    # Assert
    assert (args.lookback_hours, args.end_hours_ago) == (lookback_hours, 0)


# --- rules_failure_args: method / status / currency / country ----------------------------------------------


@pytest.mark.parametrize(
    ("question", "method"),
    [
        ("why do bank transfer payments fail", "bank_transfer"),
        ("why do wallet payments fail", "wallet"),
        ("why do upi payments fail", "upi"),
        ("why do card payments fail", "card"),
        ("why do Bank Transfer payments fail", "bank_transfer"),
        ("why do payments fail", None),
    ],
)
def test_rules_failure_args_maps_method_keywords(question: str, method: str | None) -> None:
    # Arrange / Act
    args = rules_failure_args(question)

    # Assert
    assert args.method == method


def test_rules_failure_args_prefers_bank_transfer_over_card_when_both_appear() -> None:
    # Arrange / Act
    args = rules_failure_args("card payments that failed by bank transfer")

    # Assert
    assert args.method == "bank_transfer"


@pytest.mark.parametrize(
    ("question", "status"),
    [
        ("why were payments declined", "declined"),
        ("how many declines happened", "declined"),
        ("why did payments fail", None),
        ("why were payments declined or failed", None),
        ("why are payments failing", None),
        ("show payments", None),
    ],
)
def test_rules_failure_args_sets_declined_only_when_the_question_never_says_fail(
    question: str, status: str | None
) -> None:
    # Arrange / Act
    args = rules_failure_args(question)

    # Assert
    assert args.status == status


@pytest.mark.parametrize(
    ("question", "currency"),
    [
        ("failures in USD", "USD"),
        ("failures in EUR", "EUR"),
        ("failures in INR", "INR"),
        ("failures in JPY", "JPY"),
        ("failures in GBP", "GBP"),
        ("failures in usd", "USD"),
        ("failures in Eur today", "EUR"),
        ("failures in CAD", None),
        ("failures in usdt", None),
        ("failures for everyone", None),
    ],
)
def test_rules_failure_args_matches_currency_codes_as_whole_words_in_any_case(
    question: str, currency: str | None
) -> None:
    # Arrange / Act
    args = rules_failure_args(question)

    # Assert
    assert args.currency == currency


@pytest.mark.parametrize(
    ("question", "country"),
    [
        ("failures in India", "IN"),
        ("failures in Japan", "JP"),
        ("failures in the United States", "US"),
        ("failures in the USA", "US"),
        ("failures in Germany", "DE"),
        ("failures in France", "FR"),
        ("failures in the United Kingdom", "GB"),
        ("failures in the UK", "GB"),
        ("failures in Brazil", None),
        ("failures everywhere", None),
    ],
)
def test_rules_failure_args_maps_country_names(question: str, country: str | None) -> None:
    # Arrange / Act
    args = rules_failure_args(question)

    # Assert
    assert args.country == country


def test_rules_failure_args_returns_the_defaults_for_a_question_without_keywords() -> None:
    # Arrange / Act
    args = rules_failure_args("why are payments failing")

    # Assert
    assert args == FailureQueryArgs(
        lookback_hours=24, end_hours_ago=0, method=None, status=None, country=None, currency=None
    )


def test_rules_failure_args_combines_every_field_from_one_question() -> None:
    # Arrange
    question = "why were wallet payments declined in EUR in Germany in the last 6 hours"

    # Act
    args = rules_failure_args(question)

    # Assert
    assert args == FailureQueryArgs(
        lookback_hours=6, end_hours_ago=0, method="wallet", status="declined", country="DE", currency="EUR"
    )


# --- FailureQueryArgs ------------------------------------------------------------------------------------


def test_failure_query_args_has_the_documented_defaults() -> None:
    # Arrange / Act
    args = FailureQueryArgs()

    # Assert
    assert (
        args.lookback_hours,
        args.end_hours_ago,
        args.method,
        args.status,
        args.country,
        args.currency,
    ) == (24, 0, None, None, None, None)


# --- to_failure_query -----------------------------------------------------------------------------------


def test_to_failure_query_computes_the_window_from_now_and_takes_the_merchant_from_scope() -> None:
    # Arrange
    args = FailureQueryArgs(lookback_hours=6)

    # Act
    query = to_failure_query(args, SCOPE, NOW, "rules")

    # Assert
    assert query == FailureQuery(
        merchant_id=MERCHANT,
        start=NOW - timedelta(hours=6),
        end=NOW,
        method=None,
        status=None,
        country=None,
        currency=None,
        source="rules",
        unapplied_filters=(),
    )


def test_to_failure_query_shifts_the_window_back_by_end_hours_ago() -> None:
    # Arrange
    args = FailureQueryArgs(lookback_hours=24, end_hours_ago=24)

    # Act
    query = to_failure_query(args, SCOPE, NOW, "rules")

    # Assert
    assert (query.start, query.end) == (NOW - timedelta(hours=48), NOW - timedelta(hours=24))


def test_to_failure_query_passes_method_status_currency_and_source_through() -> None:
    # Arrange
    args = FailureQueryArgs(method="upi", status="failed", currency="INR")

    # Act
    query = to_failure_query(args, SCOPE, NOW, "llm")

    # Assert
    assert (query.method, query.status, query.currency, query.source) == ("upi", "failed", "INR", "llm")


def test_to_failure_query_reports_country_as_an_unapplied_filter() -> None:
    # Arrange
    args = FailureQueryArgs(country="IN")

    # Act
    query = to_failure_query(args, SCOPE, NOW, "rules")

    # Assert
    assert (query.country, query.unapplied_filters) == ("IN", ("country",))


def test_to_failure_query_has_no_unapplied_filters_without_a_country() -> None:
    # Arrange
    args = FailureQueryArgs(method="card", currency="USD")

    # Act
    query = to_failure_query(args, SCOPE, NOW, "rules")

    # Assert
    assert query.unapplied_filters == ()


def test_to_failure_query_uses_the_maximum_window_without_error() -> None:
    # Arrange
    args = FailureQueryArgs(lookback_hours=720, end_hours_ago=0)

    # Act
    query = to_failure_query(args, SCOPE, NOW, "rules")

    # Assert
    assert query.end - query.start == timedelta(hours=720)


# --- FailureQueryArgs validation ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"lookback_hours": 0},
        {"lookback_hours": 721},
        {"end_hours_ago": -1},
        {"end_hours_ago": 721},
        {"lookback_hours": 400, "end_hours_ago": 400},
        {"method": "crypto"},
        {"status": "succeeded"},
        {"country": "india"},
        {"country": "in"},
        {"currency": "usd"},
        {"currency": "USDT"},
        {"merchant_id": "aaaaaaaa-0000-4000-8000-000000000001"},
    ],
)
def test_failure_query_args_rejects_out_of_range_malformed_and_extra_values(
    payload: dict[str, object],
) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ValueError, match="validation error"):
        FailureQueryArgs.model_validate(payload)


# --- parse_failure_query: no LLM --------------------------------------------------------------------------


def test_parse_failure_query_without_an_llm_uses_the_rules() -> None:
    # Arrange
    question = "why did upi payments decline yesterday"

    # Act
    query = parse_failure_query(question, SCOPE, NOW, None)

    # Assert
    assert query == FailureQuery(
        merchant_id=MERCHANT,
        start=NOW - timedelta(hours=48),
        end=NOW - timedelta(hours=24),
        method="upi",
        status="declined",
        country=None,
        currency=None,
        source="rules",
        unapplied_filters=(),
    )


# --- parse_failure_query: LLM path ------------------------------------------------------------------------


def test_parse_failure_query_uses_a_valid_single_submit_call_and_marks_the_source_llm() -> None:
    # Arrange
    arguments = json.dumps(
        {"lookback_hours": 3, "end_hours_ago": 1, "method": "card", "status": "declined", "currency": "EUR"}
    )
    tool_llm = FakeToolLlm(plan=_plan(_submit(arguments)))

    # Act
    query = parse_failure_query(QUESTION, SCOPE, NOW, tool_llm)

    # Assert
    assert query == FailureQuery(
        merchant_id=MERCHANT,
        start=NOW - timedelta(hours=4),
        end=NOW - timedelta(hours=1),
        method="card",
        status="declined",
        country=None,
        currency="EUR",
        source="llm",
        unapplied_filters=(),
    )


def test_parse_failure_query_applies_defaults_to_an_empty_argument_object() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_plan(_submit("{}")))

    # Act
    query = parse_failure_query(QUESTION, SCOPE, NOW, tool_llm)

    # Assert
    assert (query.start, query.end, query.source) == (NOW - timedelta(hours=24), NOW, "llm")


def test_parse_failure_query_flags_an_llm_country_as_unapplied() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_plan(_submit('{"country": "IN"}')))

    # Act
    query = parse_failure_query(QUESTION, SCOPE, NOW, tool_llm)

    # Assert
    assert (query.country, query.unapplied_filters, query.source) == ("IN", ("country",), "llm")


def test_parse_failure_query_makes_exactly_one_request_offering_only_the_submit_tool() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_plan(_submit("{}")))

    # Act
    parse_failure_query(QUESTION, SCOPE, NOW, tool_llm)

    # Assert
    assert len(tool_llm.requests) == 1
    request = tool_llm.requests[0]
    assert [tool.name for tool in request.tools] == ["submit_failure_query"]
    assert request.tools[0].parameters == FailureQueryArgs.model_json_schema()
    assert request.question == QUESTION


def test_parse_failure_query_bounds_the_question_sent_to_the_llm() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_plan(_submit("{}")))
    long_question = "why did payments fail? " * 400

    # Act
    parse_failure_query(long_question, SCOPE, NOW, tool_llm)

    # Assert
    assert len(tool_llm.requests[0].question) <= 2000
    assert long_question.startswith(tool_llm.requests[0].question)


def test_parse_failure_query_keeps_the_scope_merchant_when_the_llm_answer_is_valid() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_plan(_submit('{"lookback_hours": 2}')))

    # Act
    query = parse_failure_query(QUESTION, Scope(merchant_id=OTHER_MERCHANT), NOW, tool_llm)

    # Assert
    assert query.merchant_id == OTHER_MERCHANT


# --- parse_failure_query: every fallback to the rules ------------------------------------------------------

_FALLBACK_PLANS: list[tuple[str, ToolPlan | None]] = [
    ("no_calls", _plan()),
    ("two_valid_calls", _plan(_submit('{"lookback_hours": 2}'), _submit('{"lookback_hours": 5}'))),
    ("wrong_tool_name", _plan(ToolChoice(name="get_payment_status", arguments='{"lookback_hours": 2}'))),
    ("invalid_json", _plan(_submit("not json at all"))),
    ("truncated_json", _plan(_submit('{"lookback_hours": 2'))),
    ("empty_arguments", _plan(_submit(""))),
    ("json_array", _plan(_submit("[]"))),
    ("json_array_of_object", _plan(_submit('[{"lookback_hours": 2}]'))),
    ("json_string", _plan(_submit('"last 2 hours"'))),
    ("lookback_too_large", _plan(_submit('{"lookback_hours": 721}'))),
    ("lookback_zero", _plan(_submit('{"lookback_hours": 0}'))),
    ("window_sum_over_720", _plan(_submit('{"lookback_hours": 400, "end_hours_ago": 400}'))),
    ("unknown_method", _plan(_submit('{"method": "crypto"}'))),
    ("lowercase_currency", _plan(_submit('{"currency": "usd"}'))),
    ("wrong_type", _plan(_submit('{"lookback_hours": "three"}'))),
    ("extra_merchant_id", _plan(_submit(f'{{"lookback_hours": 2, "merchant_id": "{OTHER_MERCHANT}"}}'))),
    ("unavailable", None),
]


@pytest.mark.parametrize(
    "plan", [plan for _, plan in _FALLBACK_PLANS], ids=[name for name, _ in _FALLBACK_PLANS]
)
def test_parse_failure_query_falls_back_to_the_rules_for_every_unusable_llm_answer(
    plan: ToolPlan | None,
) -> None:
    # Arrange
    tool_llm = FakeToolLlm(unavailable=True) if plan is None else FakeToolLlm(plan=plan)

    # Act
    query = parse_failure_query(QUESTION, SCOPE, NOW, tool_llm)

    # Assert
    assert len(tool_llm.requests) == 1
    assert query == FailureQuery(
        merchant_id=MERCHANT,
        start=NOW - timedelta(hours=3),
        end=NOW,
        method="card",
        status=None,
        country=None,
        currency=None,
        source="rules",
        unapplied_filters=(),
    )


def test_parse_failure_query_never_lets_an_llm_supplied_merchant_id_change_the_scope() -> None:
    # Arrange
    arguments = f'{{"merchant_id": "{OTHER_MERCHANT}"}}'
    tool_llm = FakeToolLlm(plan=_plan(_submit(arguments)))

    # Act
    query = parse_failure_query(QUESTION, SCOPE, NOW, tool_llm)

    # Assert
    assert (query.merchant_id, query.source) == (MERCHANT, "rules")
