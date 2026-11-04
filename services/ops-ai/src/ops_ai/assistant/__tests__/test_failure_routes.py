"""Tests for POST /v1/assistant/failures and /query, written from docs/specs/failure-tools.md section 11."""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.grounding import ABSTAIN_TEXT
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError, MockLlm
from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.playbook import Playbook, load_playbook
from ops_ai.transfers.models import TransferFact
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

FAILURES_ROUTE = "/v1/assistant/failures"
QUERY_ROUTE = "/v1/assistant/query"
ROUTES = [FAILURES_ROUTE, QUERY_ROUTE]
QUESTION = "Why are card payments failing this afternoon?"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
THIRD_MERCHANT = uuid.UUID("cccccccc-0000-4000-8000-000000000003")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_USER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
WINDOW_START = TEST_NOW - timedelta(hours=6)
BASELINE_START = WINDOW_START - timedelta(days=7)
FAILURES_KEYS = {
    "query",
    "abstained",
    "mode",
    "text",
    "model",
    "comparison",
    "timeline",
    "reasons",
    "facts",
    "hypotheses",
    "how_to_confirm",
    "fixes",
    "claims",
    "tool_calls",
    "evidence",
}
QUERY_OBJECT_KEYS = {"start", "end", "method", "status", "country", "currency", "source", "unapplied_filters"}
REASON_KEYS = {"taxonomy_code", "category", "owner", "retryable", "count", "share", "sample_ids", "source"}
FIX_KEYS = {"rank", "taxonomy_code", "fix", "playbook_version", "evidence_refs", "incident_refs"}
CLAIM_KEYS = {"text", "evidence_refs"}
QUERY_KEYS = {
    "abstained",
    "mode",
    "text",
    "model",
    "planner_model",
    "claims",
    "tool_calls",
    "rejected_calls",
    "evidence",
}
FAILURES_TOOL_NAMES = [
    "compare_baseline",
    "change_timeline",
    "failure_reasons",
    "search_incidents",
    "list_failed_payments",
]

INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
    "(:incident_id, :merchant_id, 'Issuer unavailable on BIN 400000', 'Issuer timeouts on BIN 400000', "
    "'Rerouted BIN 400000 to acq_b while the issuer was down', 'notes', :started_at, :resolved_at, false)"
)


@dataclass
class KeyPair:
    private_pem: str
    public_pem: str


def _generate_key_pair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private_pem=private_pem, public_pem=public_pem)


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    return _generate_key_pair()


@pytest.fixture(scope="module")
def other_key_pair() -> KeyPair:
    return _generate_key_pair()


# --- fakes ------------------------------------------------------------------------------------------------


@dataclass
class CitingLlm:
    """Narrates with MockLlm, which cites every evidence ref."""

    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return MockLlm().complete(request)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


@dataclass
class ScriptedToolLlm:
    """Answers every choose_tools call with `plan`, or raises LlmUnavailableError when `plan` is None."""

    plan: ToolPlan | None
    requests: list[ToolChoiceRequest] = field(default_factory=list)

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        self.requests.append(request)
        if self.plan is None:
            raise LlmUnavailableError("tool llm down")
        return self.plan


def _plan(*calls: tuple[str, str]) -> ToolPlan:
    return ToolPlan(
        calls=tuple(ToolChoice(name=name, arguments=arguments) for name, arguments in calls), model="scripted"
    )


# --- seeding ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Spike:
    issuer_failures: list[uuid.UUID]
    """The ten issuer_unavailable failures, oldest first."""


def _fact(
    session: Session,
    occurred_at: datetime,
    *,
    state: str,
    card_bin: str,
    decline_code: str | None,
    merchant_id: uuid.UUID,
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


def _seed_spike(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> Spike:
    """Baseline: 200 card payments, 4 failed. Window (last 6 h): ten issuer_unavailable failures (BIN 400000),
    two card_declined failures (BIN 411111), eight successes."""
    issuer_failures: list[uuid.UUID] = []
    with session_factory.begin() as session:
        for index in range(200):
            failed = index % 50 == 0
            _fact(
                session,
                BASELINE_START + timedelta(minutes=1 + 40 * index),
                state="declined" if failed else "succeeded",
                card_bin="411111",
                decline_code="card_declined" if failed else None,
                merchant_id=merchant_id,
            )
        for minute in range(10):
            issuer_failures.append(
                _fact(
                    session,
                    TEST_NOW - timedelta(hours=1, minutes=30 - minute),
                    state="declined",
                    card_bin="400000",
                    decline_code="issuer_unavailable",
                    merchant_id=merchant_id,
                )
            )
        for at in (WINDOW_START + timedelta(hours=1), WINDOW_START + timedelta(hours=1, minutes=30)):
            _fact(
                session,
                at,
                state="declined",
                card_bin="411111",
                decline_code="card_declined",
                merchant_id=merchant_id,
            )
        for index in range(8):
            _fact(
                session,
                WINDOW_START + timedelta(minutes=5 + 45 * index),
                state="succeeded",
                card_bin="411111",
                decline_code=None,
                merchant_id=merchant_id,
            )
        session.execute(
            INSERT_INCIDENT,
            {
                "incident_id": "inc_own" if merchant_id == MERCHANT else "inc_other",
                "merchant_id": merchant_id,
                "started_at": datetime.fromisoformat("2026-09-14T13:00:00+00:00"),
                "resolved_at": datetime.fromisoformat("2026-09-14T16:40:00+00:00"),
            },
        )
    return Spike(issuer_failures)


def _seed_transfers(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        for sender, code in ((USER, "insufficient_funds"), (OTHER_USER, "kyc_verification_failed")):
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
                    rule_id="velocity_rule_canary_77",
                    kyc_tier=1,
                    limit_minor=100_000,
                    limit_currency="USD",
                    next_tier_limit_minor=1_000_000,
                    sender_balance_minor=525_000,
                    decided_at=TEST_NOW - timedelta(hours=3),
                    ingest_source="seed",
                )
            )


# --- http helpers -----------------------------------------------------------------------------------------


def _client(
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime],
    public_key_pem: str,
    llm: LlmPort,
    *,
    tool_llm: ScriptedToolLlm | None = None,
    playbook: Playbook | None = None,
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=llm,
        jwt_public_key_pem=public_key_pem,
        clock=clock,
        tool_llm=tool_llm,
        playbook=playbook if playbook is not None else load_playbook(),
    )
    return TestClient(create_app(deps))


def _claims(subject: str, extra: dict[str, object], roles: list[str]) -> dict[str, object]:
    issued_at = int(TEST_NOW.timestamp())
    return {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }


def _merchant_token(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _user_token(private_pem: str, user_id: uuid.UUID = USER) -> str:
    claims = _claims(f"user:{user_id}", {"user_id": str(user_id)}, ["consumer"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post_ok(
    client: TestClient,
    route: str,
    token: str,
    body: dict[str, object] | None = None,
) -> dict[str, object]:
    response = client.post(
        route, json=body if body is not None else {"question": QUESTION}, headers=_bearer(token)
    )
    assert response.status_code == 200
    parsed: dict[str, object] = response.json()
    return parsed


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _dicts(value: object) -> list[dict[str, object]]:
    return [_as_dict(item) for item in _as_list(value)]


def _refs(body: dict[str, object]) -> list[object]:
    return [item["ref"] for item in _dicts(body["evidence"])]


def _tool_names(body: dict[str, object]) -> list[object]:
    return [call["name"] for call in _dicts(body["tool_calls"])]


# --- POST /v1/assistant/failures: success shape -----------------------------------------------------------


def test_failures_returns_exactly_the_specified_keys_at_every_level(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert set(body) == FAILURES_KEYS
    assert set(_as_dict(body["query"])) == QUERY_OBJECT_KEYS
    assert {frozenset(reason) for reason in _dicts(body["reasons"])} == {frozenset(REASON_KEYS)}
    assert {frozenset(fix) for fix in _dicts(body["fixes"])} == {frozenset(FIX_KEYS)}
    assert {frozenset(claim) for claim in _dicts(body["claims"])} == {frozenset(CLAIM_KEYS)}
    assert {frozenset(call) for call in _dicts(body["tool_calls"])} == {frozenset({"name", "args"})}
    assert {frozenset(item) for item in _dicts(body["evidence"])} == {frozenset({"ref", "summary"})}


def test_failures_facts_hypotheses_and_how_to_confirm_are_claims(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    for name in ("facts", "hypotheses", "how_to_confirm"):
        items = _dicts(body[name])
        assert items != []
        assert {frozenset(item) for item in items} == {frozenset(CLAIM_KEYS)}


def test_failures_comparison_and_timeline_are_json_objects(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert _as_dict(body["comparison"]) != {}
    assert _as_dict(body["timeline"]) != {}


def test_failures_query_object_holds_the_parsed_window_and_filters_without_a_merchant_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    query = _as_dict(body["query"])
    assert datetime.fromisoformat(str(query["start"])) == WINDOW_START
    assert datetime.fromisoformat(str(query["end"])) == TEST_NOW
    assert {k: v for k, v in query.items() if k not in ("start", "end")} == {
        "method": "card",
        "status": None,
        "country": None,
        "currency": None,
        "source": "rules",
        "unapplied_filters": [],
    }
    assert str(MERCHANT) not in json.dumps(query)


def test_failures_query_reports_an_unapplied_country_filter(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(
        client,
        FAILURES_ROUTE,
        _merchant_token(key_pair.private_pem),
        {"question": "Why are card payments failing in India this afternoon?"},
    )

    # Assert
    query = _as_dict(body["query"])
    assert (query["country"], query["unapplied_filters"]) == ("IN", ["country"])


def test_failures_reasons_are_the_taxonomy_groups_with_samples_newest_first(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    spike = _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    first, second = _dicts(body["reasons"])
    assert {k: v for k, v in first.items() if k != "share"} == {
        "taxonomy_code": "issuer_unavailable",
        "category": "issuer",
        "owner": "issuer",
        "retryable": False,
        "count": 10,
        "sample_ids": [str(payment_id) for payment_id in reversed(spike.issuer_failures)][:5],
        "source": "rules",
    }
    assert first["share"] == pytest.approx(10 / 12)
    assert (second["taxonomy_code"], second["count"], second["category"], second["owner"]) == (
        "card_declined",
        2,
        "issuer",
        "cardholder",
    )
    assert second["share"] == pytest.approx(2 / 12)


def test_failures_fixes_come_from_the_playbook_and_cite_reason_evidence_and_the_past_incident(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    playbook = load_playbook()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    fixes = _dicts(body["fixes"])
    assert [fix["rank"] for fix in fixes] == [1, 2]
    top = fixes[0]
    assert top["taxonomy_code"] == "issuer_unavailable"
    assert top["playbook_version"] == playbook.version
    assert top["evidence_refs"] == ["reason:issuer_unavailable"]
    assert top["incident_refs"] == ["incident:inc_own"]
    assert str(top["fix"]).startswith(playbook.entries["issuer_unavailable"].fix)
    assert str(top["fix"]).endswith(
        "Previously applied: Rerouted BIN 400000 to acq_b while the issuer was down"
    )


def test_failures_uses_the_playbook_given_to_the_app(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    shipped = load_playbook()
    custom_fix = "Custom playbook fix for issuer outages."
    entries = {
        code: replace(entry, fix=custom_fix) if code == "issuer_unavailable" else entry
        for code, entry in shipped.entries.items()
    }
    custom = Playbook(version="custom-v9", entries=entries)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), playbook=custom)

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    top = _dicts(body["fixes"])[0]
    assert top["playbook_version"] == "custom-v9"
    assert str(top["fix"]).startswith(custom_fix)


def test_failures_lists_the_five_tool_calls_in_order_with_string_args(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert _tool_names(body) == FAILURES_TOOL_NAMES
    calls = _dicts(body["tool_calls"])
    assert calls[0]["args"] == {
        "start": WINDOW_START.isoformat(),
        "end": TEST_NOW.isoformat(),
        "baseline_days": "7",
    }
    assert all(isinstance(value, str) for call in calls for value in _as_dict(call["args"]).values())


def test_failures_evidence_starts_with_the_window_baseline_and_comparison_and_cites_the_reasons(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    spike = _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    refs = _refs(body)
    assert refs[:4] == [
        "aggregate:window",
        "aggregate:baseline",
        "comparison:baseline",
        "aggregate:bin:400000",
    ]
    assert {"reason:issuer_unavailable", "reason:card_declined", "incident:inc_own"} <= set(refs)
    assert f"payment:{spike.issuer_failures[-1]}" in refs


def test_failures_claims_cite_only_evidence_in_the_response(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    known = set(_refs(body))
    claims = _dicts(body["claims"])
    assert claims != []
    assert [claim for claim in claims if not set(_as_list(claim["evidence_refs"])) <= known] == []
    assert all(_as_list(claim["evidence_refs"]) != [] for claim in claims)


def test_failures_answers_with_the_llm_narration_when_it_is_grounded(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    llm = CitingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert body["abstained"] is False
    assert (body["mode"], body["model"]) == ("llm", "mock")
    assert "[aggregate:window]" in str(body["text"])
    assert len(llm.calls) == 1


def test_failures_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_analysis(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    token = _merchant_token(key_pair.private_pem)
    with_llm = _post_ok(
        _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm()), FAILURES_ROUTE, token
    )
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, DownLlm())

    # Act
    body = _post_ok(client, FAILURES_ROUTE, token)

    # Assert
    assert (body["abstained"], body["mode"], body["text"], body["model"]) == (
        False,
        "t2_fallback",
        None,
        None,
    )
    assert {k: v for k, v in body.items() if k not in ("mode", "text", "model")} == {
        k: v for k, v in with_llm.items() if k not in ("mode", "text", "model")
    }


def test_failures_for_a_merchant_without_payments_abstains_without_calling_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    llm = CitingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        FAILURES_ROUTE,
        json={"question": QUESTION},
        headers=_bearer(_merchant_token(key_pair.private_pem, THIRD_MERCHANT)),
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = _as_dict(response.json())
    assert set(body) == FAILURES_KEYS
    assert (body["abstained"], body["claims"], body["text"], body["mode"], body["model"]) == (
        True,
        [],
        ABSTAIN_TEXT,
        "t2_fallback",
        None,
    )
    assert llm.calls == []


def test_failures_sends_the_response_with_cache_control_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    response = client.post(
        FAILURES_ROUTE, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_failures_scopes_everything_to_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    own = _seed_spike(session_factory, MERCHANT)
    others = _seed_spike(session_factory, OTHER_MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    own_body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem, MERCHANT))
    others_body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem, OTHER_MERCHANT))

    # Assert
    own_text, others_text = json.dumps(own_body), json.dumps(others_body)
    assert [str(i) for i in others.issuer_failures if str(i) in own_text] == []
    assert [str(i) for i in own.issuer_failures if str(i) in others_text] == []
    assert "incident:inc_other" not in own_text
    assert "incident:inc_own" not in others_text
    assert "incident:inc_other" in others_text


def test_failures_passes_the_tool_llm_to_the_query_parser_and_uses_its_query(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    tool_llm = ScriptedToolLlm(_plan(("submit_failure_query", '{"lookback_hours": 3, "method": "wallet"}')))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    query = _as_dict(body["query"])
    assert (query["source"], query["method"]) == ("llm", "wallet")
    assert datetime.fromisoformat(str(query["start"])) == TEST_NOW - timedelta(hours=3)
    assert [spec.name for spec in tool_llm.requests[0].tools] == ["submit_failure_query"]


def test_failures_ignores_a_smuggled_merchant_id_in_the_tool_llm_query(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    own = _seed_spike(session_factory, MERCHANT)
    others = _seed_spike(session_factory, OTHER_MERCHANT)
    smuggled = json.dumps({"merchant_id": str(OTHER_MERCHANT)})
    tool_llm = ScriptedToolLlm(_plan(("submit_failure_query", smuggled)))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem, MERCHANT))

    # Assert
    text_out = json.dumps(body)
    assert _as_dict(body["query"])["source"] == "rules"
    assert [str(i) for i in others.issuer_failures if str(i) in text_out] == []
    assert f"payment:{own.issuer_failures[-1]}" in text_out


def test_failures_falls_back_to_the_rule_parser_when_the_tool_llm_is_down(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(
        session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=ScriptedToolLlm(None)
    )

    # Act
    body = _post_ok(client, FAILURES_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert _as_dict(body["query"])["source"] == "rules"
    assert body["abstained"] is False


# --- POST /v1/assistant/query: success shape --------------------------------------------------------------


def test_query_returns_exactly_the_specified_keys_at_every_level(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert set(body) == QUERY_KEYS
    assert {frozenset(claim) for claim in _dicts(body["claims"])} == {frozenset(CLAIM_KEYS)}
    assert {frozenset(call) for call in _dicts(body["tool_calls"])} == {frozenset({"name", "args"})}
    assert {frozenset(item) for item in _dicts(body["evidence"])} == {frozenset({"ref", "summary"})}


def test_query_without_a_tool_llm_plans_with_the_rules_for_a_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    llm = CitingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert body["planner_model"] == "rules"
    assert _tool_names(body) == ["compare_baseline", "change_timeline", "failure_reasons"]
    assert body["rejected_calls"] == []
    assert body["abstained"] is False
    assert (body["mode"], body["model"]) == ("llm", "mock")
    assert {
        "aggregate:window",
        "aggregate:baseline",
        "comparison:baseline",
        "reason:issuer_unavailable",
    } <= set(_refs(body))
    assert len(llm.calls) == 1


def test_query_uses_the_tool_llm_as_the_planner_and_reports_its_model(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    tool_llm = ScriptedToolLlm(_plan(("aggregate_failures", '{"lookback_hours": 6}')))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert body["planner_model"] == "scripted"
    assert _tool_names(body) == ["aggregate_failures"]
    assert _dicts(body["tool_calls"])[0]["args"] == {"lookback_hours": "6", "end_hours_ago": "0"}
    assert _refs(body) == ["aggregate:window"]
    assert [spec.name for spec in tool_llm.requests[0].tools] == [
        "get_payment_status",
        "list_failed_payments",
        "aggregate_failures",
        "compare_baseline",
        "change_timeline",
        "failure_reasons",
        "search_incidents",
        "analyze_fraud",
    ]


def test_query_falls_back_to_the_rule_planner_when_the_tool_llm_is_down(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(
        session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=ScriptedToolLlm(None)
    )

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert body["planner_model"] == "rules"
    assert _tool_names(body) == ["compare_baseline", "change_timeline", "failure_reasons"]


def test_query_lists_rejected_calls_and_still_runs_the_valid_ones(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    tool_llm = ScriptedToolLlm(
        _plan(
            ("drop_tables", "{}"),
            ("aggregate_failures", '{"lookback_hours": 0}'),
            ("aggregate_failures", "{}"),
        )
    )
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert body["rejected_calls"] == [
        {"name": "drop_tables", "reason": "unknown_tool"},
        {"name": "aggregate_failures", "reason": "invalid_args"},
    ]
    assert _tool_names(body) == ["aggregate_failures"]


def test_query_rejects_a_model_supplied_merchant_id_and_keeps_the_token_scope(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    own = _seed_spike(session_factory, MERCHANT)
    others = _seed_spike(session_factory, OTHER_MERCHANT)
    smuggled = json.dumps({"merchant_id": str(OTHER_MERCHANT)})
    tool_llm = ScriptedToolLlm(_plan(("list_failed_payments", smuggled), ("list_failed_payments", "{}")))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem, MERCHANT))

    # Assert
    text_out = json.dumps(body)
    assert body["rejected_calls"] == [{"name": "list_failed_payments", "reason": "invalid_args"}]
    assert [str(i) for i in others.issuer_failures if str(i) in text_out] == []
    assert f"payment:{own.issuer_failures[-1]}" in text_out


def test_query_scopes_the_rule_planned_answer_to_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    others = _seed_spike(session_factory, OTHER_MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem, MERCHANT))

    # Assert
    text_out = json.dumps(body)
    assert [str(i) for i in others.issuer_failures if str(i) in text_out] == []
    assert str(OTHER_MERCHANT) not in text_out


def test_query_with_an_unavailable_llm_returns_t2_fallback_with_claims_and_evidence(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, DownLlm())

    # Act
    body = _post_ok(client, QUERY_ROUTE, _merchant_token(key_pair.private_pem))

    # Assert
    assert (body["abstained"], body["mode"], body["text"], body["model"]) == (
        False,
        "t2_fallback",
        None,
        None,
    )
    assert _dicts(body["claims"]) != []
    assert "aggregate:window" in _refs(body)


def test_query_with_a_question_no_rule_matches_abstains_without_calling_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    llm = CitingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        QUERY_ROUTE, json={"question": "Hello"}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {
        "abstained": True,
        "mode": "t2_fallback",
        "text": ABSTAIN_TEXT,
        "model": None,
        "planner_model": "rules",
        "claims": [],
        "tool_calls": [],
        "rejected_calls": [],
        "evidence": [],
    }
    assert llm.calls == []


def test_query_sends_the_response_with_cache_control_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_spike(session_factory, MERCHANT)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    response = client.post(
        QUERY_ROUTE, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


# --- POST /v1/assistant/query: user token -----------------------------------------------------------------


def test_query_with_a_user_token_uses_the_user_tools_and_the_senders_transfers(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_transfers(session_factory)
    tool_llm = ScriptedToolLlm(_plan(("failure_reasons", "{}")))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(
        client, QUERY_ROUTE, _user_token(key_pair.private_pem), {"question": "Why did my transfer fail?"}
    )

    # Assert
    assert [spec.name for spec in tool_llm.requests[0].tools] == ["get_payment_status", "failure_reasons"]
    assert _refs(body) == ["reason:insufficient_funds"]
    assert body["abstained"] is False
    assert body["rejected_calls"] == []
    assert "kyc_verification_failed" not in json.dumps(body)


def test_query_with_a_user_token_rejects_a_merchant_only_tool(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_transfers(session_factory)
    tool_llm = ScriptedToolLlm(_plan(("analyze_fraud", "{}"), ("failure_reasons", "{}")))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, QUERY_ROUTE, _user_token(key_pair.private_pem))

    # Assert
    assert body["rejected_calls"] == [{"name": "analyze_fraud", "reason": "unknown_tool"}]
    assert _tool_names(body) == ["failure_reasons"]


def test_query_with_a_user_token_and_no_tool_llm_plans_with_the_rules_over_the_user_tools(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_transfers(session_factory)
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    body = _post_ok(
        client,
        QUERY_ROUTE,
        _user_token(key_pair.private_pem),
        {"question": "Is this fraud? Why did my payments fail?"},
    )

    # Assert
    assert body["planner_model"] == "rules"
    assert _tool_names(body) == ["failure_reasons"]
    assert _refs(body) == ["reason:insufficient_funds"]


def test_query_with_a_user_token_never_exposes_the_transfer_rule_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_transfers(session_factory)
    tool_llm = ScriptedToolLlm(_plan(("get_payment_status", "{}"), ("failure_reasons", "{}")))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm(), tool_llm=tool_llm)

    # Act
    body = _post_ok(client, QUERY_ROUTE, _user_token(key_pair.private_pem))

    # Assert
    assert "velocity_rule_canary_77" not in json.dumps(body)
    assert "transfer:" in "".join(str(ref) for ref in _refs(body))


# --- auth -------------------------------------------------------------------------------------------------


def test_failures_rejects_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    llm = CitingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        FAILURES_ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer garbage-token-canary-8842"}, {"Authorization": "Basic Z2FyYmFnZQ=="}],
    ids=["no_authorization_header", "garbage_token", "not_a_bearer_scheme"],
)
def test_routes_without_a_valid_token_return_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str, headers: dict[str, str]
) -> None:
    # Arrange
    llm = CitingLlm()
    tool_llm = ScriptedToolLlm(_plan(("aggregate_failures", "{}")))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm, tool_llm=tool_llm)

    # Act
    response = client.post(route, json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert "garbage-token-canary-8842" not in response.text
    assert llm.calls == []
    assert tool_llm.requests == []


@pytest.mark.parametrize("route", ROUTES)
def test_routes_reject_a_token_signed_by_another_key_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, other_key_pair: KeyPair, route: str
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    response = client.post(
        route, json={"question": QUESTION}, headers=_bearer(_merchant_token(other_key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize("route", ROUTES)
def test_routes_reject_an_expired_token_once_the_injected_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW + timedelta(seconds=901), key_pair.public_pem, CitingLlm()
    )

    # Act
    response = client.post(
        route, json={"question": QUESTION}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


# --- body validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("field_name", ["merchant_id", "user_id", "start", "limit", "tools"])
def test_routes_reject_any_extra_body_field_with_422_and_do_not_echo_the_input(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str, field_name: str
) -> None:
    # Arrange
    llm = CitingLlm()
    tool_llm = ScriptedToolLlm(_plan(("aggregate_failures", "{}")))
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm, tool_llm=tool_llm)
    body = {"question": QUESTION, field_name: str(OTHER_MERCHANT)}

    # Act
    response = client.post(route, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert _as_dict(response.json())["error"] == "validation_failed"
    assert str(OTHER_MERCHANT) not in response.text
    assert llm.calls == []
    assert tool_llm.requests == []


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "x" * 2001}, {"question": 123}, {"question": None}],
    ids=["no_question", "empty_question", "question_2001_chars", "question_not_a_string", "question_null"],
)
def test_routes_reject_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str, body: object
) -> None:
    # Arrange
    llm = CitingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(route, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert _as_dict(response.json())["error"] == "validation_failed"
    assert llm.calls == []


@pytest.mark.parametrize("route", ROUTES)
def test_routes_accept_a_question_of_exactly_2000_characters(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, CitingLlm())

    # Act
    response = client.post(
        route, json={"question": "x" * 2000}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
