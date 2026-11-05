"""RED route tests for docs/specs/evaluation-judge.md section 6.4.2 (investigation judge)."""

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.judge.fake import FakeJudgeLlm
from ops_ai.judge.hypotheses import PLAYBOOK
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

QUESTION = "15% of my card payments are failing this afternoon - what is the root cause and how do we fix it?"
ROUTE = "/v1/assistant/investigate"
WINDOW_START = TEST_NOW - timedelta(hours=6)
BASELINE_START = WINDOW_START - timedelta(days=7)
TOP_LEVEL_KEYS = {
    "intent",
    "cause_label",
    "mode",
    "text",
    "model",
    "window",
    "baseline",
    "suggested_fixes",
    "tool_calls",
    "evidence",
}


@dataclass(frozen=True)
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


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text="Investigation narration.", model="recording-model")


@dataclass
class FailingJudgeLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise RuntimeError("unexpected judge failure")


@dataclass
class SlowJudgeLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        time.sleep(0.2)
        return FakeJudgeLlm().complete(request)


@dataclass(frozen=True)
class Seeded:
    merchant_a: uuid.UUID
    merchant_b: uuid.UUID
    a_failed_ids: tuple[uuid.UUID, ...]
    b_failed_ids: tuple[uuid.UUID, ...]


def _event(
    merchant_id: uuid.UUID, occurred_at: datetime, *, declined: bool, payment_id: uuid.UUID
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=occurred_at,
        entity_version=1,
        payment_id=payment_id,
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="issuer_unavailable" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0119" if declined else "4242",
    )


def _insert_incident(session: Session, merchant_id: uuid.UUID, incident_id: str) -> None:
    session.execute(
        text(
            "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
            "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
            "(:incident_id, :merchant_id, 'Issuer outage on BIN 400000', 'Issuer timeouts on BIN 400000', "
            "'Rerouted BIN 400000 to acq_b', 'notes', :started_at, :resolved_at, false)"
        ),
        {
            "incident_id": incident_id,
            "merchant_id": merchant_id,
            "started_at": TEST_NOW - timedelta(days=50),
            "resolved_at": TEST_NOW - timedelta(days=50) + timedelta(hours=3),
        },
    )


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> Seeded:
    """Seed an issuer outage for A and separate failed payments and an incident for B."""
    merchant_a, merchant_b = uuid.uuid4(), uuid.uuid4()
    a_failed = tuple(uuid.uuid4() for _ in range(10))
    b_failed = tuple(uuid.uuid4() for _ in range(6))
    events: list[PaymentEventV1] = []
    for index, payment_id in enumerate(a_failed):
        events.append(
            _event(
                merchant_a,
                WINDOW_START + timedelta(minutes=10 * (index + 1)),
                declined=True,
                payment_id=payment_id,
            )
        )
    for index in range(10):
        events.append(
            _event(
                merchant_a,
                WINDOW_START + timedelta(minutes=5 + index),
                declined=False,
                payment_id=uuid.uuid4(),
            )
        )
    for index in range(20):
        events.append(
            _event(
                merchant_a,
                BASELINE_START + timedelta(minutes=index + 1),
                declined=False,
                payment_id=uuid.uuid4(),
            )
        )
    for index, payment_id in enumerate(b_failed):
        events.append(
            _event(
                merchant_b, WINDOW_START + timedelta(minutes=index + 1), declined=True, payment_id=payment_id
            )
        )
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
        _insert_incident(session, merchant_a, "inc_a")
        _insert_incident(session, merchant_b, "inc_b")
    return Seeded(merchant_a, merchant_b, a_failed, b_failed)


def _client(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    judge_llm: LlmPort | None,
    *,
    judge_timeout_seconds: float = 5.0,
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=RecordingLlm(),
        jwt_public_key_pem=key_pair.public_pem,
        clock=lambda: TEST_NOW,
        judge_llm=judge_llm,
        judge_timeout_seconds=judge_timeout_seconds,
    )
    return TestClient(create_app(deps))


def _token(private_pem: str, merchant_id: uuid.UUID) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _post(
    client: TestClient,
    key_pair: KeyPair,
    merchant_id: uuid.UUID,
    *,
    judge: bool = False,
    question: str = QUESTION,
) -> dict[str, object]:
    route = f"{ROUTE}?judge=true" if judge else ROUTE
    response = client.post(
        route,
        json={"question": question},
        headers={"Authorization": f"Bearer {_token(key_pair.private_pem, merchant_id)}"},
    )
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    return body


def _skipped(reason: str) -> dict[str, object]:
    return {
        "status": "skipped",
        "reason": reason,
        "cause_code": None,
        "score": None,
        "passed": None,
        "rationale": None,
        "resolution_steps": [],
        "llm_status": None,
        "model": None,
    }


def test_without_judge_parameter_preserves_old_response_keys_and_makes_no_judge_call(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    judge_llm = FakeJudgeLlm()
    client = _client(session_factory, key_pair, judge_llm)

    # Act
    body = _post(client, key_pair, seeded.merchant_a)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS
    assert body["cause_label"] == "issuer_outage_on_bin"
    assert judge_llm.requests == []


def test_judge_true_without_judge_llm_adds_exact_skipped_shape_and_preserves_other_fields(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, None)
    original = _post(client, key_pair, seeded.merchant_a)

    # Act
    body = _post(client, key_pair, seeded.merchant_a, judge=True)

    # Assert
    assert set(original) == TOP_LEVEL_KEYS
    assert set(body) == TOP_LEVEL_KEYS | {"judge"}
    assert body["judge"] == _skipped("no judge LLM configured")
    assert {key: value for key, value in body.items() if key != "judge"} == original


def test_judge_true_assesses_seeded_issuer_outage_with_fake_judge(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    judge_llm = FakeJudgeLlm()
    client = _client(session_factory, key_pair, judge_llm)
    original = _post(client, key_pair, seeded.merchant_a)

    # Act
    body = _post(client, key_pair, seeded.merchant_a, judge=True)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS | {"judge"}
    assert body["cause_label"] == "issuer_outage_on_bin"
    assert body["judge"] == {
        "status": "assessed",
        "reason": None,
        "cause_code": body["cause_label"],
        "score": 6.0,
        "passed": True,
        "rationale": None,
        "resolution_steps": list(PLAYBOOK["issuer_outage_on_bin"]),
        "llm_status": "used",
        "model": "fake-judge",
    }
    assert {key: value for key, value in body.items() if key != "judge"} == original
    assert len(judge_llm.requests) == 1


def test_runtime_error_from_judge_returns_200_with_judge_failed(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    judge_llm = FailingJudgeLlm()
    client = _client(session_factory, key_pair, judge_llm)
    original = _post(client, key_pair, seeded.merchant_a)

    # Act
    body = _post(client, key_pair, seeded.merchant_a, judge=True)

    # Assert
    assert body["judge"] == _skipped("judge failed")
    assert {key: value for key, value in body.items() if key != "judge"} == original
    assert len(judge_llm.calls) == 1


def test_slow_judge_returns_200_with_judge_timed_out(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    judge_llm = SlowJudgeLlm()
    client = _client(session_factory, key_pair, judge_llm, judge_timeout_seconds=0.05)
    original = _post(client, key_pair, seeded.merchant_a)

    # Act
    body = _post(client, key_pair, seeded.merchant_a, judge=True)

    # Assert
    assert body["judge"] == _skipped("judge timed out")
    assert {key: value for key, value in body.items() if key != "judge"} == original
    assert len(judge_llm.calls) == 1


def test_unsupported_question_skips_judge_without_calling_the_judge_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    judge_llm = FakeJudgeLlm()
    client = _client(session_factory, key_pair, judge_llm)

    # Act
    body = _post(client, key_pair, seeded.merchant_a, judge=True, question="Tell me a joke.")

    # Assert
    assert body == {
        "intent": "unsupported",
        "cause_label": "unsupported",
        "mode": "t2_fallback",
        "text": None,
        "model": None,
        "window": None,
        "baseline": None,
        "suggested_fixes": [],
        "tool_calls": [],
        "evidence": [],
        "judge": _skipped("no root-cause hypothesis to judge"),
    }
    assert judge_llm.requests == []


def test_judge_request_evidence_contains_only_the_authenticated_merchants_refs(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: Seeded
) -> None:
    # Arrange
    judge_llm = FakeJudgeLlm()
    client = _client(session_factory, key_pair, judge_llm)
    expected_refs = (
        "aggregate:window",
        "aggregate:baseline",
        "aggregate:bin:400000",
        "incident:inc_a",
        *(f"payment:{payment_id}" for payment_id in reversed(seeded.a_failed_ids[-5:])),
    )
    foreign_refs = {
        f"merchant:{seeded.merchant_b}",
        "incident:inc_b",
        *(f"payment:{payment_id}" for payment_id in seeded.b_failed_ids),
    }

    # Act
    body = _post(client, key_pair, seeded.merchant_a, judge=True)

    # Assert
    assert len(judge_llm.requests) == 1
    request_refs = tuple(item.ref for item in judge_llm.requests[0].evidence)
    assert request_refs == expected_refs
    assert set(request_refs) & foreign_refs == set()
    assert body["evidence"] == [
        {"ref": item.ref, "summary": item.summary} for item in judge_llm.requests[0].evidence
    ]
