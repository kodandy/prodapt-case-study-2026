"""Tests for ops_ai/tools/registry.py and catalog.py, written from docs/specs/failure-tools.md section 9."""

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import Claim
from ops_ai.assistant.tool_calling import ToolSpec
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope, UserScope
from ops_ai.tools.catalog import merchant_tools, user_tools
from ops_ai.tools.registry import (
    ToolContext,
    ToolDefinition,
    ToolExecutionError,
    ToolOutput,
    ToolRegistry,
    ToolRejectedError,
)
from ops_ai.transfers.models import TransferFact
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
USER_SCOPE = UserScope(user_id=SENDER)
CARD_REF = "cafecafecafecafecafecafecafecafe"
RULE_ID = "velocity_rule_canary_77"
ARG_MARKER = "ARGUMENT-LEAK-MARKER-4471"
UNKNOWN_ID = uuid.UUID("99999999-0000-4000-8000-000000000009")

MERCHANT_TOOL_NAMES = (
    "get_payment_status",
    "list_failed_payments",
    "aggregate_failures",
    "compare_baseline",
    "change_timeline",
    "failure_reasons",
    "search_incidents",
    "analyze_fraud",
)
USER_TOOL_NAMES = ("get_payment_status", "failure_reasons")
FAILURE_QUERY_FIELDS = {"lookback_hours", "end_hours_ago", "method", "status", "country", "currency"}
INCIDENT_TEXT = "Issuer timeouts on BIN 400000"
INSERT_PAYMENT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(gen_random_uuid(), :merchant_id, 100, 'USD', 'card', 'declined', 'acq_a', NULL, '400000', NULL, 1, "
    ":occurred_at)"
)
INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
    "(:incident_id, :merchant_id, :title, :root_cause, 'Rerouted traffic', 'notes', :started_at, "
    ":resolved_at, false)"
)


# --- seeding ----------------------------------------------------------------------------------------------


def _payment(
    session: Session,
    occurred_at: datetime,
    *,
    state: str,
    method: str = "card",
    card_bin: str = "411111",
    decline_code: str | None = None,
    merchant_id: uuid.UUID = MERCHANT,
    amount_minor: int = 1500,
    card_ref: str = CARD_REF,
) -> uuid.UUID:
    payment_id = uuid.uuid4()
    session.add(
        PaymentFact(
            payment_id=payment_id,
            merchant_id=merchant_id,
            amount_minor=amount_minor,
            currency="USD",
            method=method,
            state=state,
            provider="acq_a",
            decline_code=decline_code,
            card_bin=card_bin,
            card_ref=card_ref,
            entity_version=1,
            occurred_at=occurred_at,
        )
    )
    return payment_id


@dataclass(frozen=True)
class Seeded:
    wallet_failed: uuid.UUID
    """11:00 wallet, failed, no decline code."""
    issuer_failures: list[uuid.UUID]
    """10:30, 10:31, 10:32 card declined on BIN 400000 (issuer_unavailable), oldest first."""
    processing_failure: uuid.UUID
    """09:00 card failed on BIN 411111 (processing_error)."""
    success: uuid.UUID
    old_failure: uuid.UUID
    """30 h before NOW, card declined (card_declined): outside the default 24 h window."""
    other_failures: list[uuid.UUID]
    """The other merchant's failed payments inside the same hours."""


def _seed_merchants(session_factory: sessionmaker[Session]) -> Seeded:
    with session_factory.begin() as session:
        for hour in range(20):
            _payment(session, NOW - timedelta(hours=6, days=3) + timedelta(hours=hour), state="succeeded")
        success = _payment(session, NOW - timedelta(hours=4), state="succeeded")
        processing = _payment(
            session, NOW - timedelta(hours=3), state="failed", decline_code="processing_error"
        )
        issuer = [
            _payment(
                session,
                NOW - timedelta(hours=1, minutes=30 - minute),
                state="declined",
                card_bin="400000",
                decline_code="issuer_unavailable",
            )
            for minute in range(3)
        ]
        wallet = _payment(session, NOW - timedelta(hours=1), state="failed", method="wallet")
        old_failure = _payment(
            session, NOW - timedelta(hours=30), state="declined", decline_code="card_declined"
        )
        others = [
            _payment(
                session,
                NOW - timedelta(hours=1, minutes=20 - minute),
                state="declined",
                card_bin="400000",
                decline_code="do_not_honor",
                merchant_id=OTHER_MERCHANT,
            )
            for minute in range(4)
        ]
        for merchant_id, incident_id in (
            (MERCHANT, "inc_own"),
            (None, "inc_platform"),
            (OTHER_MERCHANT, "inc_other"),
        ):
            session.execute(
                INSERT_INCIDENT,
                {
                    "incident_id": incident_id,
                    "merchant_id": merchant_id,
                    "title": INCIDENT_TEXT,
                    "root_cause": INCIDENT_TEXT,
                    "started_at": NOW - timedelta(days=40),
                    "resolved_at": NOW - timedelta(days=39),
                },
            )
    return Seeded(wallet, issuer, processing, success, old_failure, others)


def _transfer(
    session: Session,
    decided_at: datetime,
    *,
    status: str,
    reason_code: str | None,
    sender: uuid.UUID = SENDER,
    rule_id: str | None = RULE_ID,
) -> uuid.UUID:
    transfer_id = uuid.uuid4()
    session.add(
        TransferFact(
            transfer_id=transfer_id,
            sender_user_id=sender,
            amount_minor=180_000,
            currency="USD",
            method="bank_transfer",
            status=status,
            country_sender="US",
            country_receiver="IN",
            reason_code=reason_code,
            rule_id=rule_id,
            kyc_tier=1,
            limit_minor=100_000,
            limit_currency="USD",
            next_tier_limit_minor=1_000_000,
            sender_balance_minor=525_000,
            decided_at=decided_at,
            ingest_source="seed",
        )
    )
    return transfer_id


@dataclass(frozen=True)
class SeededTransfers:
    funds_old: uuid.UUID
    funds_newer: uuid.UUID
    limit_newest_failed: uuid.UUID
    flagged: uuid.UUID
    other_senders: uuid.UUID


def _seed_transfers(session_factory: sessionmaker[Session]) -> SeededTransfers:
    with session_factory.begin() as session:
        funds_old = _transfer(
            session, NOW - timedelta(days=2), status="failed", reason_code="insufficient_funds"
        )
        funds_newer = _transfer(
            session, NOW - timedelta(hours=30), status="failed", reason_code="insufficient_funds"
        )
        limit_failed = _transfer(
            session, NOW - timedelta(hours=12), status="failed", reason_code="cross_border_limit_kyc_tier"
        )
        flagged = _transfer(session, NOW - timedelta(hours=1), status="flagged", reason_code=None)
        other = _transfer(
            session,
            NOW - timedelta(hours=2),
            status="failed",
            reason_code="kyc_verification_failed",
            sender=OTHER_SENDER,
        )
    return SeededTransfers(funds_old, funds_newer, limit_failed, flagged, other)


# --- helpers ----------------------------------------------------------------------------------------------


def _merchant_invoke(
    session_factory: sessionmaker[Session], name: str, arguments: str = "{}", scope: Scope = SCOPE
) -> ToolOutput:
    with session_factory() as session:
        return merchant_tools().invoke(session, scope, NOW, name, arguments)


def _user_invoke(
    session_factory: sessionmaker[Session],
    name: str,
    arguments: str = "{}",
    scope: UserScope = USER_SCOPE,
) -> ToolOutput:
    with session_factory() as session:
        return user_tools().invoke(session, scope, NOW, name, arguments)


def _refs(output: ToolOutput) -> list[str]:
    return [item.ref for item in output.evidence]


def _claim_refs(output: ToolOutput) -> list[tuple[str, ...]]:
    return [claim.evidence_refs for claim in output.claims]


def _all_text(output: ToolOutput) -> str:
    parts = [f"{item.ref} {item.summary}" for item in output.evidence]
    parts += [f"{claim.text} {' '.join(claim.evidence_refs)}" for claim in output.claims]
    return "\n".join(parts)


def _rejection(registry: ToolRegistry[Scope] | ToolRegistry[UserScope], name: str, arguments: str) -> str:
    with pytest.raises(ToolRejectedError) as exc:
        registry.validate(name, arguments)
    return exc.value.reason


def _property_names(spec: ToolSpec) -> set[str]:
    properties = spec.parameters["properties"]
    assert isinstance(properties, dict)
    return set(properties)


def _payment_count(session_factory: sessionmaker[Session]) -> int:
    with session_factory() as session:
        return int(session.execute(text("SELECT count(*) FROM ops_ai.payment_facts")).scalar_one())


class CountArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int = Field(default=1, ge=1, le=5)
    label: str | None = None


@dataclass
class Recorder:
    contexts: list[ToolContext[Scope]] = field(default_factory=list)
    arguments: list[BaseModel] = field(default_factory=list)


def _recording_definition(name: str, recorder: Recorder, output: ToolOutput) -> ToolDefinition[Scope]:
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        recorder.contexts.append(context)
        recorder.arguments.append(args)
        return output

    return ToolDefinition(name=name, description=f"{name} description", args_model=CountArgs, run=run)


ECHO_OUTPUT = ToolOutput(
    evidence=(EvidenceItem(ref="echo:1", summary="echoed"),),
    claims=(Claim(text="Echo happened", evidence_refs=("echo:1",)),),
)


# --- names and specs --------------------------------------------------------------------------------------


def test_merchant_tools_names_are_exactly_the_specified_tools_in_order() -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    names = registry.names()

    # Assert
    assert names == MERCHANT_TOOL_NAMES


def test_user_tools_names_are_exactly_the_specified_tools_in_order() -> None:
    # Arrange
    registry = user_tools()

    # Act
    names = registry.names()

    # Assert
    assert names == USER_TOOL_NAMES


def test_specs_follow_the_name_order_and_describe_every_tool() -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    specs = registry.specs()

    # Assert
    assert [spec.name for spec in specs] == list(MERCHANT_TOOL_NAMES)
    assert [spec.name for spec in specs if not spec.description.strip()] == []
    assert all(isinstance(spec, ToolSpec) for spec in specs)


@pytest.mark.parametrize(
    ("name", "properties", "required"),
    [
        pytest.param("get_payment_status", {"payment_id"}, ["payment_id"], id="get_payment_status"),
        pytest.param("list_failed_payments", FAILURE_QUERY_FIELDS | {"limit"}, [], id="list_failed_payments"),
        pytest.param("aggregate_failures", {"lookback_hours", "end_hours_ago"}, [], id="aggregate_failures"),
        pytest.param("compare_baseline", {"window_hours", "baseline_days"}, [], id="compare_baseline"),
        pytest.param(
            "change_timeline",
            {"window_hours", "bucket_minutes", "baseline_days"},
            [],
            id="change_timeline",
        ),
        pytest.param("failure_reasons", FAILURE_QUERY_FIELDS, [], id="failure_reasons"),
        pytest.param("search_incidents", {"query"}, ["query"], id="search_incidents"),
        pytest.param("analyze_fraud", {"lookback_hours"}, [], id="analyze_fraud"),
    ],
)
def test_merchant_specs_offer_only_the_specified_arguments_and_forbid_extra_keys(
    name: str, properties: set[str], required: list[str]
) -> None:
    # Arrange
    spec = {spec.name: spec for spec in merchant_tools().specs()}[name]

    # Act
    schema = spec.parameters

    # Assert
    assert _property_names(spec) == properties
    assert schema.get("required", []) == required
    assert schema["additionalProperties"] is False


@pytest.mark.parametrize(
    ("name", "properties"),
    [
        pytest.param("get_payment_status", {"transfer_id"}, id="get_payment_status"),
        pytest.param("failure_reasons", {"lookback_hours"}, id="failure_reasons"),
    ],
)
def test_user_specs_offer_only_the_specified_arguments_and_forbid_extra_keys(
    name: str, properties: set[str]
) -> None:
    # Arrange
    spec = {spec.name: spec for spec in user_tools().specs()}[name]

    # Act
    schema = spec.parameters

    # Assert
    assert _property_names(spec) == properties
    assert schema.get("required", []) == []
    assert schema["additionalProperties"] is False


def test_specs_parameters_equal_the_json_schema_of_the_args_model() -> None:
    # Arrange
    registry = ToolRegistry([_recording_definition("echo", Recorder(), ECHO_OUTPUT)])

    # Act
    specs = registry.specs()

    # Assert
    assert specs == (
        ToolSpec(name="echo", description="echo description", parameters=CountArgs.model_json_schema()),
    )


def test_registry_names_follow_the_construction_order() -> None:
    # Arrange
    recorder = Recorder()
    registry = ToolRegistry(
        [
            _recording_definition("zeta", recorder, ECHO_OUTPUT),
            _recording_definition("alpha", recorder, ECHO_OUTPUT),
        ]
    )

    # Act
    names = registry.names()

    # Assert
    assert names == ("zeta", "alpha")


def test_registry_rejects_a_duplicate_tool_name_with_value_error() -> None:
    # Arrange
    recorder = Recorder()
    definitions = [
        _recording_definition("echo", recorder, ECHO_OUTPUT),
        _recording_definition("echo", recorder, ECHO_OUTPUT),
    ]

    # Act
    with pytest.raises(ValueError):
        ToolRegistry(definitions)

    # Assert
    assert recorder.contexts == []


# --- validate: accepted arguments -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "defaults"),
    [
        pytest.param(
            "aggregate_failures", {"lookback_hours": 6, "end_hours_ago": 0}, id="aggregate_failures"
        ),
        pytest.param("compare_baseline", {"window_hours": 6, "baseline_days": 7}, id="compare_baseline"),
        pytest.param(
            "change_timeline",
            {"window_hours": 6, "bucket_minutes": 60, "baseline_days": 7},
            id="change_timeline",
        ),
        pytest.param("analyze_fraud", {"lookback_hours": 24}, id="analyze_fraud"),
        pytest.param(
            "list_failed_payments",
            {
                "lookback_hours": 24,
                "end_hours_ago": 0,
                "method": None,
                "status": None,
                "country": None,
                "currency": None,
                "limit": 20,
            },
            id="list_failed_payments",
        ),
        pytest.param(
            "failure_reasons",
            {
                "lookback_hours": 24,
                "end_hours_ago": 0,
                "method": None,
                "status": None,
                "country": None,
                "currency": None,
            },
            id="failure_reasons",
        ),
    ],
)
def test_validate_fills_the_specified_defaults_for_an_empty_object(
    name: str, defaults: dict[str, object]
) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    args = registry.validate(name, "{}")

    # Assert
    assert args.model_dump() == defaults


def test_validate_user_tool_defaults() -> None:
    # Arrange
    registry = user_tools()

    # Act
    status_args = registry.validate("get_payment_status", "{}")
    reasons_args = registry.validate("failure_reasons", "{}")

    # Assert
    assert status_args.model_dump() == {"transfer_id": None}
    assert reasons_args.model_dump() == {"lookback_hours": 720}


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        pytest.param(
            "aggregate_failures",
            '{"lookback_hours": 1, "end_hours_ago": 0}',
            {"lookback_hours": 1, "end_hours_ago": 0},
            id="lookback-min",
        ),
        pytest.param(
            "aggregate_failures",
            '{"lookback_hours": 720, "end_hours_ago": 720}',
            {"lookback_hours": 720, "end_hours_ago": 720},
            id="max-values",
        ),
        pytest.param(
            "compare_baseline",
            '{"window_hours": 72, "baseline_days": 30}',
            {"window_hours": 72, "baseline_days": 30},
            id="compare-max",
        ),
        pytest.param("change_timeline", '{"bucket_minutes": 15}', {"bucket_minutes": 15}, id="bucket-15"),
        pytest.param("change_timeline", '{"bucket_minutes": 30}', {"bucket_minutes": 30}, id="bucket-30"),
        pytest.param("list_failed_payments", '{"limit": 50}', {"limit": 50}, id="limit-50"),
        pytest.param("list_failed_payments", '{"limit": 1}', {"limit": 1}, id="limit-1"),
        pytest.param("analyze_fraud", '{"lookback_hours": 168}', {"lookback_hours": 168}, id="fraud-168"),
        pytest.param(
            "search_incidents", json.dumps({"query": "q" * 200}), {"query": "q" * 200}, id="query-200"
        ),
        pytest.param(
            "failure_reasons",
            '{"country": "IN", "currency": "USD"}',
            {"country": "IN", "currency": "USD"},
            id="country-currency",
        ),
        pytest.param(
            "failure_reasons",
            '{"method": "upi", "status": "failed"}',
            {"method": "upi", "status": "failed"},
            id="method-status",
        ),
        pytest.param(
            "failure_reasons",
            '{"lookback_hours": 700, "end_hours_ago": 20}',
            {"lookback_hours": 700, "end_hours_ago": 20},
            id="sum-at-720",
        ),
        pytest.param(
            "get_payment_status",
            json.dumps({"payment_id": str(UNKNOWN_ID)}),
            {"payment_id": str(UNKNOWN_ID)},
            id="payment-id",
        ),
    ],
)
def test_validate_accepts_boundary_arguments(name: str, arguments: str, expected: dict[str, object]) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    args = registry.validate(name, arguments)

    # Assert
    dumped = args.model_dump(mode="json")
    assert {key: dumped[key] for key in expected} == expected


def test_validate_accepts_arguments_of_exactly_4096_characters() -> None:
    # Arrange
    base = '{"query": "x"}'
    arguments = base[:-1] + " " * (4096 - len(base)) + "}"
    assert len(arguments) == 4096

    # Act
    args = merchant_tools().validate("search_incidents", arguments)

    # Assert
    assert args.model_dump() == {"query": "x"}


def test_validate_returns_the_parsed_values() -> None:
    # Arrange
    arguments = '{"lookback_hours": 3, "end_hours_ago": 2}'

    # Act
    args = merchant_tools().validate("aggregate_failures", arguments)

    # Assert
    assert args.model_dump() == {"lookback_hours": 3, "end_hours_ago": 2}


# --- validate: rejected arguments -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["no_such_tool", "", "analyze_fraud ", "ANALYZE_FRAUD", "drop_table_payment_facts"],
)
def test_validate_rejects_an_unknown_tool_name(name: str) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    reason = _rejection(registry, name, "{}")

    # Assert
    assert reason == "unknown_tool"


def test_validate_rejects_a_merchant_only_tool_on_the_user_registry_as_unknown() -> None:
    # Arrange
    registry = user_tools()

    # Act
    reasons = [_rejection(registry, name, "{}") for name in ("analyze_fraud", "search_incidents")]

    # Assert
    assert reasons == ["unknown_tool", "unknown_tool"]


def test_validate_checks_the_name_before_the_arguments() -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    reason = _rejection(registry, "no_such_tool", "not json")

    # Assert
    assert reason == "unknown_tool"


def test_validate_rejects_arguments_longer_than_4096_characters() -> None:
    # Arrange
    base = '{"query": "x"}'
    arguments = base[:-1] + " " * (4097 - len(base)) + "}"
    assert len(arguments) == 4097

    # Act
    reason = _rejection(merchant_tools(), "search_incidents", arguments)

    # Assert
    assert reason == "invalid_args"


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param("", id="empty-string"),
        pytest.param("not json", id="plain-text"),
        pytest.param("{'lookback_hours': 6}", id="single-quotes"),
        pytest.param('{"lookback_hours": 6', id="truncated"),
        pytest.param('{"lookback_hours": NaN}', id="nan"),
        pytest.param("[]", id="empty-array"),
        pytest.param('[{"lookback_hours": 6}]', id="array-of-object"),
        pytest.param('"lookback_hours"', id="string"),
        pytest.param("42", id="number"),
        pytest.param("null", id="null"),
        pytest.param("true", id="boolean"),
    ],
)
def test_validate_rejects_arguments_that_are_not_a_json_object(arguments: str) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    reason = _rejection(registry, "aggregate_failures", arguments)

    # Assert
    assert reason == "invalid_args"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param("aggregate_failures", '{"lookback_hours": 0}', id="lookback-0"),
        pytest.param("aggregate_failures", '{"lookback_hours": 721}', id="lookback-721"),
        pytest.param("aggregate_failures", '{"end_hours_ago": -1}', id="end-negative"),
        pytest.param("aggregate_failures", '{"end_hours_ago": 721}', id="end-721"),
        pytest.param("aggregate_failures", '{"lookback_hours": "many"}', id="lookback-not-a-number"),
        pytest.param("aggregate_failures", '{"lookback_hours": 1.5}', id="lookback-fractional"),
        pytest.param("compare_baseline", '{"window_hours": 0}', id="window-0"),
        pytest.param("compare_baseline", '{"window_hours": 73}', id="window-73"),
        pytest.param("compare_baseline", '{"baseline_days": 0}', id="baseline-0"),
        pytest.param("compare_baseline", '{"baseline_days": 31}', id="baseline-31"),
        pytest.param("change_timeline", '{"bucket_minutes": 20}', id="bucket-20"),
        pytest.param("change_timeline", '{"bucket_minutes": 120}', id="bucket-120"),
        pytest.param("change_timeline", '{"window_hours": 73}', id="timeline-window-73"),
        pytest.param("list_failed_payments", '{"limit": 0}', id="limit-0"),
        pytest.param("list_failed_payments", '{"limit": 51}', id="limit-51"),
        pytest.param("list_failed_payments", '{"method": "crypto"}', id="method-unknown"),
        pytest.param("list_failed_payments", '{"status": "succeeded"}', id="status-not-failed"),
        pytest.param("failure_reasons", '{"country": "india"}', id="country-name"),
        pytest.param("failure_reasons", '{"country": "in"}', id="country-lower"),
        pytest.param("failure_reasons", '{"currency": "usd"}', id="currency-lower"),
        pytest.param("failure_reasons", '{"currency": "US"}', id="currency-two-letters"),
        pytest.param(
            "failure_reasons", '{"lookback_hours": 720, "end_hours_ago": 1}', id="window-over-720-in-total"
        ),
        pytest.param("search_incidents", "{}", id="query-missing"),
        pytest.param("search_incidents", '{"query": ""}', id="query-empty"),
        pytest.param("search_incidents", json.dumps({"query": "q" * 201}), id="query-201"),
        pytest.param("analyze_fraud", '{"lookback_hours": 0}', id="fraud-0"),
        pytest.param("analyze_fraud", '{"lookback_hours": 169}', id="fraud-169"),
        pytest.param("get_payment_status", "{}", id="payment-id-missing"),
        pytest.param("get_payment_status", '{"payment_id": "not-a-uuid"}', id="payment-id-bad"),
        pytest.param("get_payment_status", '{"payment_id": null}', id="payment-id-null"),
    ],
)
def test_validate_rejects_arguments_that_break_the_schema(name: str, arguments: str) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    reason = _rejection(registry, name, arguments)

    # Assert
    assert reason == "invalid_args"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        pytest.param("failure_reasons", '{"lookback_hours": 0}', id="lookback-0"),
        pytest.param("failure_reasons", '{"lookback_hours": 721}', id="lookback-721"),
        pytest.param("get_payment_status", '{"transfer_id": "nope"}', id="transfer-id-bad"),
    ],
)
def test_validate_rejects_user_tool_arguments_that_break_the_schema(name: str, arguments: str) -> None:
    # Arrange
    registry = user_tools()

    # Act
    reason = _rejection(registry, name, arguments)

    # Assert
    assert reason == "invalid_args"


VALID_BASE_ARGUMENTS: dict[str, dict[str, object]] = {
    "get_payment_status": {"payment_id": str(UNKNOWN_ID)},
    "list_failed_payments": {},
    "aggregate_failures": {},
    "compare_baseline": {},
    "change_timeline": {},
    "failure_reasons": {},
    "search_incidents": {"query": "issuer"},
    "analyze_fraud": {},
}
SCOPE_KEYS = {
    "merchant_id": str(OTHER_MERCHANT),
    "user_id": str(SENDER),
    "scope": {"merchant_id": str(OTHER_MERCHANT)},
}


@pytest.mark.parametrize("key", sorted(SCOPE_KEYS))
@pytest.mark.parametrize("name", MERCHANT_TOOL_NAMES)
def test_validate_rejects_a_scope_key_in_the_arguments_of_every_merchant_tool(name: str, key: str) -> None:
    # Arrange
    arguments = json.dumps({**VALID_BASE_ARGUMENTS[name], key: SCOPE_KEYS[key]})

    # Act
    reason = _rejection(merchant_tools(), name, arguments)

    # Assert
    assert reason == "invalid_args"


@pytest.mark.parametrize("key", sorted(SCOPE_KEYS))
@pytest.mark.parametrize("name", USER_TOOL_NAMES)
def test_validate_rejects_a_scope_key_in_the_arguments_of_every_user_tool(name: str, key: str) -> None:
    # Arrange
    arguments = json.dumps({key: SCOPE_KEYS[key]})

    # Act
    reason = _rejection(user_tools(), name, arguments)

    # Assert
    assert reason == "invalid_args"


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(f"not json {ARG_MARKER}", id="not-json"),
        pytest.param(json.dumps([ARG_MARKER]), id="array"),
        pytest.param(json.dumps({"lookback_hours": 0, ARG_MARKER: 1}), id="extra-key-and-range"),
        pytest.param(json.dumps({"lookback_hours": ARG_MARKER}), id="bad-type"),
        pytest.param(json.dumps({"note": ARG_MARKER * 600}), id="too-long"),
        pytest.param(json.dumps({"merchant_id": ARG_MARKER}), id="scope-key"),
    ],
)
def test_validate_error_never_contains_the_argument_text(arguments: str) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    with pytest.raises(ToolRejectedError) as exc:
        registry.validate("aggregate_failures", arguments)

    # Assert
    assert exc.value.reason == "invalid_args"
    assert ARG_MARKER not in str(exc.value)
    assert ARG_MARKER not in repr(exc.value)
    assert ARG_MARKER not in repr(exc.value.args)


# --- invoke: mechanics ------------------------------------------------------------------------------------


def test_invoke_runs_the_tool_with_the_scope_now_session_and_validated_args(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = ToolRegistry([_recording_definition("echo", recorder, ECHO_OUTPUT)])

    # Act
    with session_factory() as session:
        output = registry.invoke(session, SCOPE, NOW, "echo", '{"count": 3}')

        # Assert
        assert output == ECHO_OUTPUT
        (context,) = recorder.contexts
        assert context.session is session
        assert context.scope == SCOPE
        assert context.now == NOW
        assert recorder.arguments == [CountArgs(count=3)]


def test_invoke_does_not_run_the_tool_when_the_arguments_are_invalid(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = ToolRegistry([_recording_definition("echo", recorder, ECHO_OUTPUT)])

    # Act
    with session_factory() as session, pytest.raises(ToolRejectedError) as exc:
        registry.invoke(session, SCOPE, NOW, "echo", '{"count": 6}')

    # Assert
    assert exc.value.reason == "invalid_args"
    assert recorder.contexts == []


def test_invoke_rejects_an_unknown_tool_without_running_anything(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = ToolRegistry([_recording_definition("echo", recorder, ECHO_OUTPUT)])

    # Act
    with session_factory() as session, pytest.raises(ToolRejectedError) as exc:
        registry.invoke(session, SCOPE, NOW, "other", "{}")

    # Assert
    assert exc.value.reason == "unknown_tool"
    assert recorder.contexts == []


def test_invoke_runs_the_tool_inside_a_read_only_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        mode = context.session.execute(text("SHOW transaction_read_only")).scalar_one()
        return ToolOutput(evidence=(EvidenceItem(ref="tx:1", summary=str(mode)),), claims=())

    registry = ToolRegistry(
        [ToolDefinition(name="probe", description="probe", args_model=CountArgs, run=run)]
    )

    # Act
    with session_factory() as session:
        output = registry.invoke(session, SCOPE, NOW, "probe", "{}")

    # Assert
    assert [item.summary for item in output.evidence] == ["on"]


def test_invoke_rolls_back_and_leaves_the_session_without_a_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    registry = merchant_tools()

    # Act
    with session_factory() as session:
        registry.invoke(session, SCOPE, NOW, "aggregate_failures", "{}")
        in_transaction = session.in_transaction()

    # Assert
    assert in_transaction is False


def test_invoke_raises_value_error_and_runs_nothing_when_the_session_already_has_a_transaction(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    recorder = Recorder()
    registry = ToolRegistry([_recording_definition("echo", recorder, ECHO_OUTPUT)])

    # Act
    with session_factory() as session:
        session.execute(text("SELECT 1"))
        with pytest.raises(ValueError):
            registry.invoke(session, SCOPE, NOW, "echo", "{}")

    # Assert
    assert recorder.contexts == []


def test_invoke_turns_a_write_attempt_into_tool_execution_error_and_writes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        context.session.execute(INSERT_PAYMENT, {"merchant_id": MERCHANT, "occurred_at": NOW})
        return ToolOutput(evidence=(), claims=())

    registry = ToolRegistry(
        [ToolDefinition(name="writer", description="tries to write", args_model=CountArgs, run=run)]
    )

    # Act
    with session_factory() as session, pytest.raises(ToolExecutionError) as exc:
        registry.invoke(session, SCOPE, NOW, "writer", "{}")

    # Assert
    assert exc.value.tool == "writer"
    assert _payment_count(session_factory) == 0


def test_invoke_does_not_persist_an_orm_insert_made_by_a_tool(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        _payment(context.session, NOW, state="declined")
        context.session.flush()
        return ToolOutput(evidence=(), claims=())

    registry = ToolRegistry(
        [ToolDefinition(name="orm_writer", description="tries to write", args_model=CountArgs, run=run)]
    )

    # Act
    with session_factory() as session, pytest.raises(ToolExecutionError):
        registry.invoke(session, SCOPE, NOW, "orm_writer", "{}")

    # Assert
    assert _payment_count(session_factory) == 0


def test_invoke_turns_any_database_error_into_tool_execution_error_for_that_tool(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    def run(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        context.session.execute(text("SELECT * FROM ops_ai.table_that_does_not_exist"))
        return ToolOutput(evidence=(), claims=())

    registry = ToolRegistry(
        [ToolDefinition(name="broken", description="broken", args_model=CountArgs, run=run)]
    )

    # Act
    with session_factory() as session, pytest.raises(ToolExecutionError) as exc:
        registry.invoke(session, SCOPE, NOW, "broken", "{}")

    # Assert
    assert exc.value.tool == "broken"


def test_invoke_still_works_on_the_same_session_after_a_failed_tool(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    def broken(context: ToolContext[Scope], args: BaseModel) -> ToolOutput:
        context.session.execute(text("SELECT * FROM ops_ai.table_that_does_not_exist"))
        return ToolOutput(evidence=(), claims=())

    recorder = Recorder()
    registry = ToolRegistry(
        [
            ToolDefinition(name="broken", description="broken", args_model=CountArgs, run=broken),
            _recording_definition("echo", recorder, ECHO_OUTPUT),
        ]
    )

    # Act
    with session_factory() as session:
        with pytest.raises(ToolExecutionError):
            registry.invoke(session, SCOPE, NOW, "broken", "{}")
        output = registry.invoke(session, SCOPE, NOW, "echo", "{}")

    # Assert
    assert output == ECHO_OUTPUT


# --- merchant tools: scope isolation ----------------------------------------------------------------------


def test_get_payment_status_returns_empty_output_for_another_merchants_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)
    others_payment = seeded.other_failures[0]

    # Act
    output = _merchant_invoke(
        session_factory, "get_payment_status", json.dumps({"payment_id": str(others_payment)})
    )

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_get_payment_status_returns_empty_output_for_an_unknown_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(
        session_factory, "get_payment_status", json.dumps({"payment_id": str(UNKNOWN_ID)})
    )

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_get_payment_status_serves_the_other_merchant_its_own_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)
    others_payment = seeded.other_failures[0]

    # Act
    output = _merchant_invoke(
        session_factory, "get_payment_status", json.dumps({"payment_id": str(others_payment)}), OTHER_SCOPE
    )

    # Assert
    assert _refs(output) == [f"payment:{others_payment}"]


def test_list_failed_payments_never_returns_another_merchants_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "list_failed_payments", '{"lookback_hours": 720}')

    # Assert
    text_out = _all_text(output)
    assert [str(payment_id) for payment_id in seeded.other_failures if str(payment_id) in text_out] == []
    assert "do_not_honor" not in text_out


def test_list_failed_payments_for_the_other_merchant_returns_only_its_own_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "list_failed_payments", "{}", OTHER_SCOPE)

    # Assert
    assert _refs(output) == [
        "aggregate:failed_payments",
        *[f"payment:{payment_id}" for payment_id in reversed(seeded.other_failures)],
    ]


# --- merchant tools: evidence ----------------------------------------------------------------------------


def test_get_payment_status_returns_the_payment_as_evidence_with_one_claim(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)
    payment_id = seeded.issuer_failures[2]

    # Act
    output = _merchant_invoke(
        session_factory, "get_payment_status", json.dumps({"payment_id": str(payment_id)})
    )

    # Assert
    ref = f"payment:{payment_id}"
    assert _refs(output) == [ref]
    assert _claim_refs(output) == [(ref,)]
    summary = output.evidence[0].summary
    assert "declined" in summary.lower()
    assert "issuer_unavailable" in summary
    assert "USD" in summary


def test_get_payment_status_output_carries_no_card_reference(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(
        session_factory, "get_payment_status", json.dumps({"payment_id": str(seeded.issuer_failures[0])})
    )

    # Assert
    assert CARD_REF not in _all_text(output)


def test_list_failed_payments_returns_the_count_then_the_failed_payments_newest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "list_failed_payments")

    # Assert
    assert _refs(output) == [
        "aggregate:failed_payments",
        f"payment:{seeded.wallet_failed}",
        *[f"payment:{payment_id}" for payment_id in reversed(seeded.issuer_failures)],
        f"payment:{seeded.processing_failure}",
    ]
    assert _claim_refs(output) == [("aggregate:failed_payments",)]
    assert "5" in output.evidence[0].summary


def test_list_failed_payments_excludes_succeeded_payments_and_failures_outside_the_window(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "list_failed_payments")

    # Assert
    refs = _refs(output)
    assert f"payment:{seeded.success}" not in refs
    assert f"payment:{seeded.old_failure}" not in refs


def test_list_failed_payments_includes_an_older_failure_with_a_longer_lookback(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "list_failed_payments", '{"lookback_hours": 48}')

    # Assert
    assert _refs(output)[-1] == f"payment:{seeded.old_failure}"


def test_list_failed_payments_applies_the_method_and_status_filters(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    wallet = _merchant_invoke(session_factory, "list_failed_payments", '{"method": "wallet"}')
    declined = _merchant_invoke(session_factory, "list_failed_payments", '{"status": "declined"}')

    # Assert
    assert _refs(wallet) == ["aggregate:failed_payments", f"payment:{seeded.wallet_failed}"]
    assert _refs(declined) == [
        "aggregate:failed_payments",
        *[f"payment:{payment_id}" for payment_id in reversed(seeded.issuer_failures)],
    ]


def test_list_failed_payments_limit_keeps_the_newest_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "list_failed_payments", '{"limit": 2}')

    # Assert
    assert _refs(output)[1:] == [f"payment:{seeded.wallet_failed}", f"payment:{seeded.issuer_failures[2]}"]


def test_aggregate_failures_returns_the_window_aggregate_with_one_claim(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "aggregate_failures")

    # Assert
    assert _refs(output) == ["aggregate:window"]
    assert _claim_refs(output) == [("aggregate:window",)]


def test_aggregate_failures_summaries_differ_between_merchants_with_different_data(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    own = _merchant_invoke(session_factory, "aggregate_failures")
    others = _merchant_invoke(session_factory, "aggregate_failures", scope=OTHER_SCOPE)
    nobody = _merchant_invoke(
        session_factory, "aggregate_failures", scope=Scope(merchant_id=uuid.UUID(int=12345))
    )

    # Assert
    assert len({own.evidence[0].summary, others.evidence[0].summary, nobody.evidence[0].summary}) == 3


def test_compare_baseline_returns_window_baseline_and_comparison_with_one_claim_citing_all_three(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "compare_baseline")

    # Assert
    assert _refs(output) == ["aggregate:window", "aggregate:baseline", "comparison:baseline"]
    assert len(output.claims) == 1
    assert set(output.claims[0].evidence_refs) == {
        "aggregate:window",
        "aggregate:baseline",
        "comparison:baseline",
    }


def test_change_timeline_returns_one_ref_per_change_and_one_per_bucket_with_failures(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)
    change_refs = {
        "change:failure_onset:card",
        "change:new_decline_code:issuer_unavailable",
        "change:new_decline_code:processing_error",
        "change:bin_onset:400000",
    }
    bucket_refs = {
        f"timeline:{datetime(2026, 11, 3, 9, 0, tzinfo=UTC).isoformat()}",
        f"timeline:{datetime(2026, 11, 3, 10, 0, tzinfo=UTC).isoformat()}",
    }

    # Act
    output = _merchant_invoke(session_factory, "change_timeline")

    # Assert
    refs = _refs(output)
    assert {ref for ref in refs if ref.startswith("change:")} == change_refs
    assert {ref for ref in refs if ref.startswith("timeline:")} == bucket_refs
    assert len(refs) == len(set(refs)) == 6
    claim_refs = _claim_refs(output)
    assert [claim_refs.count((ref,)) for ref in sorted(change_refs)] == [1, 1, 1, 1]


def test_change_timeline_returns_no_evidence_for_a_merchant_without_card_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "change_timeline", scope=Scope(merchant_id=uuid.UUID(int=777)))

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_failure_reasons_returns_one_reason_ref_and_claim_per_group_by_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "failure_reasons")

    # Assert
    assert _refs(output) == ["reason:issuer_unavailable", "reason:processing_error", "reason:unclassified"]
    assert _claim_refs(output) == [
        ("reason:issuer_unavailable",),
        ("reason:processing_error",),
        ("reason:unclassified",),
    ]
    summary = output.evidence[0].summary
    assert "issuer" in summary
    assert "3" in summary


def test_failure_reasons_applies_the_method_filter(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "failure_reasons", '{"method": "card"}')

    # Assert
    assert _refs(output) == ["reason:issuer_unavailable", "reason:processing_error"]


def test_failure_reasons_never_counts_another_merchants_failures(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    own = _merchant_invoke(session_factory, "failure_reasons", '{"lookback_hours": 720}')
    others = _merchant_invoke(session_factory, "failure_reasons", scope=OTHER_SCOPE)

    # Assert
    assert "reason:do_not_honor" not in _refs(own)
    assert _refs(others) == ["reason:do_not_honor"]


def test_search_incidents_returns_own_and_platform_incidents_with_one_claim_each(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "search_incidents", json.dumps({"query": "issuer timeouts"}))

    # Assert
    assert set(_refs(output)) == {"incident:inc_own", "incident:inc_platform"}
    assert len(output.evidence) == 2
    assert sorted(_claim_refs(output)) == [("incident:inc_own",), ("incident:inc_platform",)]


def test_search_incidents_returns_nothing_for_a_query_that_matches_no_incident(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "search_incidents", json.dumps({"query": "zebra"}))

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_analyze_fraud_returns_one_episode_ref_and_claim_per_flagged_episode_of_the_scope(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 12 micro card payments 10 s apart on 4 cards, 6 declined; the same burst for another merchant
    card_refs = [f"{index % 4 + 1:032x}" for index in range(12)]
    burst_start = NOW - timedelta(hours=2)
    with session_factory.begin() as session:
        own = [
            _payment(
                session,
                burst_start + timedelta(seconds=10 * index),
                state="declined" if index < 6 else "succeeded",
                card_bin="400000",
                amount_minor=50,
                card_ref=card_refs[index],
            )
            for index in range(12)
        ]
        others = [
            _payment(
                session,
                burst_start + timedelta(seconds=10 * index),
                state="declined" if index < 6 else "succeeded",
                card_bin="400000",
                amount_minor=50,
                card_ref=card_refs[index],
                merchant_id=OTHER_MERCHANT,
            )
            for index in range(12)
        ]

    # Act
    output = _merchant_invoke(session_factory, "analyze_fraud")

    # Assert
    ref = f"episode:{own[0]}"
    rendered = _all_text(output)
    assert _refs(output) == [ref]
    assert _claim_refs(output) == [(ref,)]
    assert str(others[0]) not in rendered
    assert [card_ref for card_ref in card_refs if card_ref in rendered] == []


def test_analyze_fraud_returns_nothing_when_there_is_no_flagged_episode(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_merchants(session_factory)

    # Act
    output = _merchant_invoke(session_factory, "analyze_fraud")

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_no_merchant_tool_output_carries_a_card_ref_or_rule_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_merchants(session_factory)
    calls = {
        "get_payment_status": json.dumps({"payment_id": str(seeded.processing_failure)}),
        "list_failed_payments": '{"lookback_hours": 720}',
        "aggregate_failures": "{}",
        "compare_baseline": "{}",
        "change_timeline": "{}",
        "failure_reasons": '{"lookback_hours": 720}',
        "search_incidents": json.dumps({"query": "issuer timeouts"}),
        "analyze_fraud": "{}",
    }

    # Act
    outputs = [_merchant_invoke(session_factory, name, arguments) for name, arguments in calls.items()]

    # Assert
    leaks = [name for name, output in zip(calls, outputs, strict=True) if CARD_REF in _all_text(output)]
    assert leaks == []


# --- user tools -------------------------------------------------------------------------------------------


def test_user_get_payment_status_returns_the_transfer_with_the_sender_facing_status(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_transfers(session_factory)

    # Act
    output = _user_invoke(
        session_factory, "get_payment_status", json.dumps({"transfer_id": str(seeded.limit_newest_failed)})
    )

    # Assert
    ref = f"transfer:{seeded.limit_newest_failed}"
    assert _refs(output) == [ref]
    assert _claim_refs(output) == [(ref,)]
    assert "failed" in output.evidence[0].summary.lower()


def test_user_get_payment_status_shows_a_fraud_hold_as_pending_and_never_the_rule_or_flag(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_transfers(session_factory)

    # Act
    output = _user_invoke(
        session_factory, "get_payment_status", json.dumps({"transfer_id": str(seeded.flagged)})
    )

    # Assert
    rendered = _all_text(output)
    assert _refs(output) == [f"transfer:{seeded.flagged}"]
    assert "pending" in rendered.lower()
    assert "flagged" not in rendered.lower()
    assert RULE_ID not in rendered
    assert "rule_id" not in rendered


def test_user_get_payment_status_without_an_id_returns_the_newest_failed_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_transfers(session_factory)

    # Act
    output = _user_invoke(session_factory, "get_payment_status")

    # Assert
    assert _refs(output) == [f"transfer:{seeded.limit_newest_failed}"]


def test_user_get_payment_status_returns_empty_output_for_another_senders_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    seeded = _seed_transfers(session_factory)

    # Act
    output = _user_invoke(
        session_factory, "get_payment_status", json.dumps({"transfer_id": str(seeded.other_senders)})
    )

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_user_get_payment_status_returns_empty_output_for_an_unknown_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_transfers(session_factory)

    # Act
    output = _user_invoke(session_factory, "get_payment_status", json.dumps({"transfer_id": str(UNKNOWN_ID)}))

    # Assert
    assert output == ToolOutput(evidence=(), claims=())


def test_user_failure_reasons_groups_the_senders_failed_transfers_by_count(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_transfers(session_factory)

    # Act
    output = _user_invoke(session_factory, "failure_reasons")

    # Assert
    assert _refs(output) == ["reason:insufficient_funds", "reason:cross_border_limit_kyc_tier"]
    assert _claim_refs(output) == [
        ("reason:insufficient_funds",),
        ("reason:cross_border_limit_kyc_tier",),
    ]


def test_user_failure_reasons_never_includes_another_senders_failures_or_the_rule_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_transfers(session_factory)

    # Act
    output = _user_invoke(session_factory, "failure_reasons")

    # Assert
    rendered = _all_text(output)
    assert "kyc_verification_failed" not in rendered
    assert RULE_ID not in rendered
    assert "flagged" not in rendered.lower()


def test_user_failure_reasons_respects_the_lookback_window(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_transfers(session_factory)

    # Act
    output = _user_invoke(session_factory, "failure_reasons", '{"lookback_hours": 24}')

    # Assert
    assert _refs(output) == ["reason:cross_border_limit_kyc_tier"]


def test_user_failure_reasons_for_the_other_sender_shows_only_its_own_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_transfers(session_factory)

    # Act
    output = _user_invoke(session_factory, "failure_reasons", scope=UserScope(user_id=OTHER_SENDER))

    # Assert
    assert _refs(output) == ["reason:kyc_verification_failed"]
