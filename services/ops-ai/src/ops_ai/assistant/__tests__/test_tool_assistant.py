"""Tests for ops_ai/assistant/tool_assistant.py, written from docs/specs/failure-tools.md section 10."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import ABSTAIN_TEXT, Claim
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError, MockLlm
from ops_ai.assistant.tool_assistant import MAX_TOOL_CALLS, RejectedCall, ToolAnswer, answer_with_tools
from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import Scope, UserScope
from ops_ai.tools.catalog import merchant_tools, user_tools
from ops_ai.tools.registry import ToolContext, ToolDefinition, ToolOutput, ToolRegistry
from ops_ai.transfers.models import TransferFact
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
QUESTION = "Why are card payments failing in the last 6 hours?"
PLANNER_MODEL = "scripted-planner-1"
LLM_MODEL = "scripted-llm-1"
UNKNOWN_ID = uuid.UUID("99999999-0000-4000-8000-000000000009")
INSERT_PAYMENT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(gen_random_uuid(), :merchant_id, 100, 'USD', 'card', 'declined', 'acq_a', NULL, '400000', NULL, 1, "
    ":occurred_at)"
)


# --- fakes ------------------------------------------------------------------------------------------------


@dataclass
class ScriptedPlanner:
    """Answers with `plan`, or raises LlmUnavailableError when `plan` is None."""

    plan: ToolPlan | None
    requests: list[ToolChoiceRequest] = field(default_factory=list)

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        self.requests.append(request)
        if self.plan is None:
            raise LlmUnavailableError("planner down")
        return self.plan


@dataclass
class RecordingLlm:
    """Narrates with MockLlm, which cites every evidence ref."""

    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return MockLlm().complete(request)


@dataclass
class FixedTextLlm:
    narration: str
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=self.narration, model=LLM_MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


Llm = RecordingLlm | FixedTextLlm | DownLlm


def _plan(*calls: tuple[str, str], model: str = PLANNER_MODEL) -> ToolPlan:
    return ToolPlan(
        calls=tuple(ToolChoice(name=name, arguments=arguments) for name, arguments in calls), model=model
    )


class CountArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(default=1, ge=1, le=5)


@dataclass
class Recorder:
    contexts: list[ToolContext[Scope]] = field(default_factory=list)


def _counting_definition(name: str, recorder: Recorder, *, grounded: bool = True) -> ToolDefinition[Scope]:
    """Records every run; each run returns one evidence item `<name>:<run number>` and one claim."""

    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        recorder.contexts.append(context)
        ref = f"{name}:{len(recorder.contexts)}"
        cited = ref if grounded else "ghost:0"
        return ToolOutput(
            evidence=(EvidenceItem(ref=ref, summary=f"{name} ran"),),
            claims=(Claim(text=f"{name} claim", evidence_refs=(cited,)),),
        )

    return ToolDefinition(name=name, description=f"{name} description", args_model=CountArgs, run=run)


def _broken_definition(name: str = "broken") -> ToolDefinition[Scope]:
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        context.session.execute(text("SELECT * FROM ops_ai.table_that_does_not_exist"))
        return ToolOutput(evidence=(), claims=())

    return ToolDefinition(name=name, description="always fails", args_model=CountArgs, run=run)


def _writer_definition(name: str = "writer") -> ToolDefinition[Scope]:
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        context.session.execute(INSERT_PAYMENT, {"merchant_id": MERCHANT, "occurred_at": NOW})
        return ToolOutput(evidence=(), claims=())

    return ToolDefinition(name=name, description="tries to write", args_model=CountArgs, run=run)


def _custom_registry(*definitions: ToolDefinition[Scope]) -> ToolRegistry[Scope]:
    return ToolRegistry(list(definitions))


# --- seeding ----------------------------------------------------------------------------------------------


def _payment(
    session: Session,
    occurred_at: datetime,
    *,
    state: str,
    decline_code: str | None,
    merchant_id: uuid.UUID,
    card_bin: str = "400000",
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    session.add(
        PaymentFact(
            payment_id=payment_id,
            merchant_id=merchant_id,
            amount_minor=1500,
            currency="USD",
            method="card",
            state=state,
            provider="acq_a",
            decline_code=decline_code,
            card_bin=card_bin,
            entity_version=1,
            occurred_at=occurred_at,
        )
    )
    return payment_id


@dataclass(frozen=True)
class Seeded:
    own_failures: list[uuid.UUID]
    others_failures: list[uuid.UUID]


def _seed(session_factory: sessionmaker[Session]) -> Seeded:
    with session_factory.begin() as session:
        for hour in range(20):
            _payment(
                session,
                NOW - timedelta(hours=6, days=3) + timedelta(hours=hour),
                state="succeeded",
                decline_code=None,
                merchant_id=MERCHANT,
                card_bin="411111",
            )
        own = [
            _payment(
                session,
                NOW - timedelta(hours=1, minutes=30 - minute),
                state="declined",
                decline_code="issuer_unavailable",
                merchant_id=MERCHANT,
            )
            for minute in range(3)
        ]
        others = [
            _payment(
                session,
                NOW - timedelta(hours=1, minutes=20 - minute),
                state="declined",
                decline_code="do_not_honor",
                merchant_id=OTHER_MERCHANT,
            )
            for minute in range(2)
        ]
    return Seeded(own, others)


# --- helpers ----------------------------------------------------------------------------------------------


def _answer(
    session_factory: sessionmaker[Session],
    planner: ScriptedPlanner,
    llm: Llm | None = None,
    *,
    registry: ToolRegistry[Scope] | None = None,
    scope: Scope = SCOPE,
    question: str = QUESTION,
) -> ToolAnswer:
    return answer_with_tools(
        session_factory,
        scope,
        question,
        NOW,
        registry if registry is not None else merchant_tools(),
        planner,
        llm if llm is not None else RecordingLlm(),
    )


def _refs(answer: ToolAnswer) -> list[str]:
    return [item.ref for item in answer.evidence]


def _assert_abstained(answer: ToolAnswer, llm: Llm) -> None:
    assert answer.abstained is True
    assert answer.claims == ()
    assert answer.text == ABSTAIN_TEXT
    assert answer.mode == "t2_fallback"
    assert answer.model is None
    assert llm.calls == []


# --- constants and shapes ---------------------------------------------------------------------------------


def test_max_tool_calls_is_four() -> None:
    # Arrange / Act
    limit = MAX_TOOL_CALLS

    # Assert
    assert limit == 4


def test_rejected_call_holds_the_tool_name_and_the_reason() -> None:
    # Arrange / Act
    rejected = RejectedCall(name="ghost", reason="unknown_tool")

    # Assert
    assert (rejected.name, rejected.reason) == ("ghost", "unknown_tool")


# --- the planner request ----------------------------------------------------------------------------------


def test_the_planner_is_asked_once_with_the_question_and_the_registry_specs(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    planner = ScriptedPlanner(_plan())
    registry = merchant_tools()

    # Act
    _answer(session_factory, planner, registry=registry)

    # Assert
    (request,) = planner.requests
    assert request.question == QUESTION
    assert request.tools == registry.specs()
    assert request.instructions.strip() != ""


def test_the_planner_instructions_are_fixed_and_carry_neither_question_nor_scope(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first, second = ScriptedPlanner(_plan()), ScriptedPlanner(_plan())

    # Act
    _answer(session_factory, first, question="first question marker-1")
    _answer(session_factory, second, question="second question marker-2", scope=OTHER_SCOPE)

    # Assert
    instructions = first.requests[0].instructions
    assert instructions == second.requests[0].instructions
    assert "marker-1" not in instructions
    assert str(MERCHANT) not in instructions


def test_the_planner_gets_the_question_cut_to_2000_characters(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    planner = ScriptedPlanner(_plan())
    question = "q" * 2500

    # Act
    _answer(session_factory, planner, question=question)

    # Assert
    assert planner.requests[0].question == "q" * 2000


def test_the_planner_gets_the_user_registry_specs_for_a_user_registry(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    planner = ScriptedPlanner(_plan())
    registry = user_tools()

    # Act
    answer_with_tools(
        session_factory, UserScope(user_id=SENDER), QUESTION, NOW, registry, planner, RecordingLlm()
    )

    # Assert
    assert [spec.name for spec in planner.requests[0].tools] == ["get_payment_status", "failure_reasons"]


# --- accepted calls ---------------------------------------------------------------------------------------


def test_accepted_calls_are_recorded_in_plan_order_with_string_arguments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory)
    planner = ScriptedPlanner(
        _plan(
            ("aggregate_failures", '{"lookback_hours": 3}'),
            ("search_incidents", '{"query": "issuer timeouts"}'),
        )
    )

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.tool_calls == (
        ToolCall(name="aggregate_failures", args={"lookback_hours": "3", "end_hours_ago": "0"}),
        ToolCall(name="search_incidents", args={"query": "issuer timeouts"}),
    )
    assert answer.rejected_calls == ()


def test_accepted_call_arguments_come_from_the_validated_model_with_defaults_and_without_none_values(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory)
    planner = ScriptedPlanner(_plan(("list_failed_payments", '{"method": "card"}')))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.tool_calls == (
        ToolCall(
            name="list_failed_payments",
            args={"lookback_hours": "24", "end_hours_ago": "0", "method": "card", "limit": "20"},
        ),
    )


def test_accepted_call_arguments_render_a_uuid_as_its_canonical_string(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed(session_factory)
    payment_id = seeded.own_failures[0]
    planner = ScriptedPlanner(_plan(("get_payment_status", json.dumps({"payment_id": str(payment_id)}))))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.tool_calls == (ToolCall(name="get_payment_status", args={"payment_id": str(payment_id)}),)
    assert all(isinstance(value, str) for call in answer.tool_calls for value in call.args.values())


def test_the_recorded_arguments_are_not_the_raw_model_text(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed(session_factory)
    planner = ScriptedPlanner(_plan(("aggregate_failures", '{ "lookback_hours" :   3 }')))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.tool_calls[0].args == {"lookback_hours": "3", "end_hours_ago": "0"}


def test_planner_model_is_the_model_the_planner_reported(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    planner = ScriptedPlanner(_plan(("aggregate_failures", "{}"), model=PLANNER_MODEL))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.planner_model == PLANNER_MODEL


def test_tool_evidence_and_claims_are_collected_in_call_order(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(
        _counting_definition("first", recorder), _counting_definition("second", recorder)
    )
    planner = ScriptedPlanner(_plan(("second", "{}"), ("first", "{}")))

    # Act
    answer = _answer(session_factory, planner, registry=registry)

    # Assert
    assert _refs(answer) == ["second:1", "first:2"]
    assert [claim.text for claim in answer.claims] == ["second claim", "first claim"]
    assert [claim.evidence_refs for claim in answer.claims] == [("second:1",), ("first:2",)]


def test_every_call_runs_in_its_own_fresh_session_with_the_callers_scope_and_now(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    planner = ScriptedPlanner(_plan(("echo", "{}"), ("echo", '{"count": 2}'), ("echo", "{}")))

    # Act
    _answer(session_factory, planner, registry=registry, scope=OTHER_SCOPE)

    # Assert
    sessions = [context.session for context in recorder.contexts]
    assert len(sessions) == 3
    assert sessions[0] is not sessions[1]
    assert sessions[1] is not sessions[2]
    assert sessions[0] is not sessions[2]
    assert [context.scope for context in recorder.contexts] == [OTHER_SCOPE] * 3
    assert [context.now for context in recorder.contexts] == [NOW] * 3


# --- rejected calls ---------------------------------------------------------------------------------------


def test_an_unknown_tool_is_rejected_and_the_other_calls_still_run(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory)
    planner = ScriptedPlanner(_plan(("delete_everything", "{}"), ("aggregate_failures", "{}")))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.rejected_calls == (RejectedCall(name="delete_everything", reason="unknown_tool"),)
    assert [call.name for call in answer.tool_calls] == ["aggregate_failures"]
    assert _refs(answer) == ["aggregate:window"]


def test_invalid_arguments_are_rejected_with_the_tool_name(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed(session_factory)
    planner = ScriptedPlanner(_plan(("aggregate_failures", '{"lookback_hours": 0}')))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.rejected_calls == (RejectedCall(name="aggregate_failures", reason="invalid_args"),)
    assert answer.tool_calls == ()


@pytest.mark.parametrize(
    ("name", "arguments", "reason"),
    [
        pytest.param("ghost", "{}", "unknown_tool", id="unknown-tool"),
        pytest.param("echo", '{"count": 99}', "invalid_args", id="out-of-range"),
        pytest.param("echo", "not json", "invalid_args", id="not-json"),
        pytest.param("echo", "[1]", "invalid_args", id="json-array"),
        pytest.param("echo", f'{{"merchant_id": "{OTHER_MERCHANT}"}}', "invalid_args", id="scope-key"),
        pytest.param("echo", '{"count": 1, "extra": true}', "invalid_args", id="extra-key"),
        pytest.param("echo", "x" * 5000, "invalid_args", id="over-4096-characters"),
    ],
)
def test_a_rejected_call_is_recorded_and_never_run_and_the_answer_abstains(
    session_factory: sessionmaker[Session], name: str, arguments: str, reason: str
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    planner = ScriptedPlanner(_plan((name, arguments)))
    llm = RecordingLlm()

    # Act
    answer = _answer(session_factory, planner, llm, registry=registry)

    # Assert
    assert [(call.name, call.reason) for call in answer.rejected_calls] == [(name, reason)]
    assert answer.tool_calls == ()
    assert recorder.contexts == []
    _assert_abstained(answer, llm)


def test_a_model_supplied_merchant_id_is_rejected_and_the_scope_stays_the_callers(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed(session_factory)
    smuggled = json.dumps({"merchant_id": str(OTHER_MERCHANT)})
    planner = ScriptedPlanner(_plan(("list_failed_payments", smuggled), ("list_failed_payments", "{}")))

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.rejected_calls == (RejectedCall(name="list_failed_payments", reason="invalid_args"),)
    assert len(answer.tool_calls) == 1
    refs = _refs(answer)
    assert [
        f"payment:{payment_id}" for payment_id in seeded.others_failures if f"payment:{payment_id}" in refs
    ] == []
    assert set(refs) >= {f"payment:{payment_id}" for payment_id in seeded.own_failures}


def test_the_same_plan_run_as_the_other_merchant_sees_only_that_merchants_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed(session_factory)
    planner = ScriptedPlanner(_plan(("list_failed_payments", "{}")))

    # Act
    answer = _answer(session_factory, planner, scope=OTHER_SCOPE)

    # Assert
    assert set(_refs(answer)) == {
        "aggregate:failed_payments",
        *[f"payment:{payment_id}" for payment_id in seeded.others_failures],
    }


def test_the_fifth_and_later_calls_are_limit_exceeded_and_never_run(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder, late_recorder = Recorder(), Recorder()
    registry = _custom_registry(
        _counting_definition("echo", recorder), _counting_definition("late", late_recorder)
    )
    planner = ScriptedPlanner(
        _plan(
            ("echo", "{}"),
            ("echo", "{}"),
            ("echo", "{}"),
            ("echo", "{}"),
            ("late", "{}"),
            ("echo", "{}"),
        )
    )

    # Act
    answer = _answer(session_factory, planner, registry=registry)

    # Assert
    assert len(recorder.contexts) == 4
    assert late_recorder.contexts == []
    assert answer.rejected_calls == (
        RejectedCall(name="late", reason="limit_exceeded"),
        RejectedCall(name="echo", reason="limit_exceeded"),
    )
    assert [call.name for call in answer.tool_calls] == ["echo"] * 4


def test_exactly_four_calls_are_all_accepted(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    planner = ScriptedPlanner(_plan(*[("echo", "{}")] * 4))

    # Act
    answer = _answer(session_factory, planner, registry=registry)

    # Assert
    assert len(recorder.contexts) == 4
    assert answer.rejected_calls == ()
    assert len(answer.tool_calls) == 4


def test_an_execution_failure_is_recorded_as_execution_failed_and_the_next_call_still_runs(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_broken_definition(), _counting_definition("echo", recorder))
    planner = ScriptedPlanner(_plan(("broken", "{}"), ("echo", "{}")))

    # Act
    answer = _answer(session_factory, planner, registry=registry)

    # Assert
    assert answer.rejected_calls == (RejectedCall(name="broken", reason="execution_failed"),)
    assert [call.name for call in answer.tool_calls] == ["echo"]
    assert _refs(answer) == ["echo:1"]


def test_a_tool_that_tries_to_write_is_execution_failed_and_nothing_is_written(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    registry = _custom_registry(_writer_definition())
    planner = ScriptedPlanner(_plan(("writer", "{}")))

    # Act
    answer = _answer(session_factory, planner, registry=registry)

    # Assert
    assert answer.rejected_calls == (RejectedCall(name="writer", reason="execution_failed"),)
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM ops_ai.payment_facts")).scalar_one() == 0


# --- planner failure --------------------------------------------------------------------------------------


def test_an_unavailable_planner_falls_back_to_the_rule_planner_and_reports_rules(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed(session_factory)
    planner = ScriptedPlanner(None)

    # Act
    answer = _answer(session_factory, planner)

    # Assert
    assert answer.planner_model == "rules"
    assert [call.name for call in answer.tool_calls] == [
        "compare_baseline",
        "change_timeline",
        "failure_reasons",
    ]
    assert answer.rejected_calls == ()
    assert len(planner.requests) == 1


def test_the_rule_planner_fallback_only_offers_the_registry_tools(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    planner = ScriptedPlanner(None)
    llm = RecordingLlm()

    # Act
    answer = answer_with_tools(
        session_factory,
        UserScope(user_id=SENDER),
        "is this fraud? was there an incident? why did my transfers fail?",
        NOW,
        user_tools(),
        planner,
        llm,
    )

    # Assert
    assert [call.name for call in answer.tool_calls] == ["get_payment_status", "failure_reasons"]
    assert answer.rejected_calls == ()
    assert answer.planner_model == "rules"


def test_an_empty_plan_abstains_without_calling_the_llm(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed(session_factory)
    llm = RecordingLlm()

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan()), llm)

    # Assert
    assert answer.tool_calls == ()
    assert answer.rejected_calls == ()
    assert answer.evidence == ()
    assert answer.planner_model == PLANNER_MODEL
    _assert_abstained(answer, llm)


# --- grounding --------------------------------------------------------------------------------------------


def test_a_tool_that_finds_nothing_abstains_but_the_accepted_call_is_still_recorded(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed(session_factory)
    arguments = json.dumps({"payment_id": str(seeded.others_failures[0])})
    llm = RecordingLlm()

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan(("get_payment_status", arguments))), llm)

    # Assert
    assert answer.tool_calls == (
        ToolCall(name="get_payment_status", args={"payment_id": str(seeded.others_failures[0])}),
    )
    assert answer.evidence == ()
    _assert_abstained(answer, llm)


def test_claims_that_cite_no_evidence_of_this_turn_are_dropped_and_the_answer_abstains(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder, grounded=False))
    llm = RecordingLlm()

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan(("echo", "{}"))), llm, registry=registry)

    # Assert
    assert [item.ref for item in answer.evidence] == ["echo:1"]
    _assert_abstained(answer, llm)


def test_only_the_grounded_claims_are_kept_in_order(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        return ToolOutput(
            evidence=(EvidenceItem(ref="mixed:1", summary="mixed"),),
            claims=(
                Claim(text="grounded one", evidence_refs=("mixed:1",)),
                Claim(text="made up", evidence_refs=("ghost:1",)),
                Claim(text="half grounded", evidence_refs=("mixed:1", "ghost:1")),
                Claim(text="no refs", evidence_refs=()),
                Claim(text="grounded two", evidence_refs=("mixed:1",)),
            ),
        )

    registry = _custom_registry(
        ToolDefinition(name="mixed", description="mixed", args_model=CountArgs, run=run)
    )
    llm = FixedTextLlm("Mixed results [mixed:1]")

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan(("mixed", "{}"))), llm, registry=registry)

    # Assert
    assert answer.abstained is False
    assert answer.claims == (
        Claim(text="grounded one", evidence_refs=("mixed:1",)),
        Claim(text="grounded two", evidence_refs=("mixed:1",)),
    )


def test_a_claim_may_cite_evidence_from_a_different_tool_of_the_same_turn(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        return ToolOutput(
            evidence=(),
            claims=(Claim(text="cross citation", evidence_refs=("provider:1",)),),
        )

    recorder = Recorder()
    registry = _custom_registry(
        _counting_definition("provider", recorder),
        ToolDefinition(name="citer", description="cites", args_model=CountArgs, run=run),
    )
    planner = ScriptedPlanner(_plan(("provider", "{}"), ("citer", "{}")))

    # Act
    answer = _answer(session_factory, planner, FixedTextLlm("Done [provider:1]"), registry=registry)

    # Assert
    assert [claim.text for claim in answer.claims] == ["provider claim", "cross citation"]


# --- narration --------------------------------------------------------------------------------------------


def test_a_grounded_narration_is_kept_with_the_llm_model(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    llm = FixedTextLlm("The echo tool ran [echo:1]")

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan(("echo", "{}"))), llm, registry=registry)

    # Assert
    assert answer.abstained is False
    assert answer.mode == "llm"
    assert answer.text == "The echo tool ran [echo:1]"
    assert answer.model == LLM_MODEL


def test_the_llm_is_called_once_with_the_question_and_all_the_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    llm = FixedTextLlm("The echo tool ran [echo:1] and [echo:2]")

    # Act
    answer = _answer(
        session_factory, ScriptedPlanner(_plan(("echo", "{}"), ("echo", "{}"))), llm, registry=registry
    )

    # Assert
    assert llm.calls == [LlmRequest(question=QUESTION, evidence=answer.evidence)]
    assert _refs(answer) == ["echo:1", "echo:2"]


@pytest.mark.parametrize(
    "narration",
    [
        pytest.param("The echo tool ran without citing anything", id="no-citation"),
        pytest.param("The echo tool ran [echo:99]", id="unknown-ref"),
        pytest.param("The echo tool ran [echo:1] and [ghost:7]", id="one-known-one-unknown"),
        pytest.param("", id="blank"),
    ],
)
def test_an_ungrounded_narration_is_dropped_and_the_evidence_and_claims_stay(
    session_factory: sessionmaker[Session], narration: str
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    llm = FixedTextLlm(narration)

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan(("echo", "{}"))), llm, registry=registry)

    # Assert
    assert answer.abstained is False
    assert answer.mode == "t2_fallback"
    assert answer.text is None
    assert _refs(answer) == ["echo:1"]
    assert [claim.text for claim in answer.claims] == ["echo claim"]
    assert len(llm.calls) == 1


def test_an_unavailable_llm_gives_t2_fallback_with_the_claims_and_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = _custom_registry(_counting_definition("echo", recorder))
    llm = DownLlm()

    # Act
    answer = _answer(session_factory, ScriptedPlanner(_plan(("echo", "{}"))), llm, registry=registry)

    # Assert
    assert answer.abstained is False
    assert answer.mode == "t2_fallback"
    assert answer.text is None
    assert answer.model is None
    assert [claim.text for claim in answer.claims] == ["echo claim"]
    assert _refs(answer) == ["echo:1"]


def test_the_real_catalog_answer_is_grounded_end_to_end(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    seeded = _seed(session_factory)
    llm = RecordingLlm()

    # Act
    answer = _answer(session_factory, ScriptedPlanner(None), llm)

    # Assert
    assert answer.abstained is False
    assert answer.mode == "llm"
    assert answer.model == "mock"
    assert "aggregate:window" in _refs(answer)
    assert "reason:issuer_unavailable" in _refs(answer)
    assert answer.claims != ()
    assert [
        ref
        for ref in _refs(answer)
        if ref.startswith("payment:") and ref.removeprefix("payment:") in map(str, seeded.others_failures)
    ] == []
    assert len(llm.calls) == 1


# --- user registry ----------------------------------------------------------------------------------------


def test_a_user_registry_answer_is_scoped_to_the_sender(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    other_sender = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
    with session_factory.begin() as session:
        for sender, code in ((SENDER, "insufficient_funds"), (other_sender, "kyc_verification_failed")):
            session.add(
                TransferFact(
                    transfer_id=uuid.uuid4(),
                    sender_user_id=sender,
                    amount_minor=180_000,
                    currency="USD",
                    method="bank_transfer",
                    status="failed",
                    country_sender="US",
                    country_receiver="IN",
                    reason_code=code,
                    rule_id="rule_canary",
                    kyc_tier=1,
                    limit_minor=100_000,
                    limit_currency="USD",
                    next_tier_limit_minor=1_000_000,
                    sender_balance_minor=525_000,
                    decided_at=NOW - timedelta(days=1),
                    ingest_source="seed",
                )
            )
    planner = ScriptedPlanner(_plan(("failure_reasons", "{}"), ("analyze_fraud", "{}")))

    # Act
    answer = answer_with_tools(
        session_factory, UserScope(user_id=SENDER), QUESTION, NOW, user_tools(), planner, RecordingLlm()
    )

    # Assert
    assert _refs(answer) == ["reason:insufficient_funds"]
    assert answer.rejected_calls == (RejectedCall(name="analyze_fraud", reason="unknown_tool"),)
