"""Tests for ops_ai.observability.llm.InstrumentedLlm, from the NFR spec C (llm.py)."""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.main import build_app_from_env
from ops_ai.observability.llm import InstrumentedLlm
from ops_ai.observability.metrics import REGISTRY
from ops_ai.observability.tracing import configure_tracing

MODEL = "recording-model-1"
DOWN_MODEL = "down-model-1"
OUTPUT_TEXT = "nine char"
QUESTION = "abcd"
# Per-part rounding would give 1 + 1 + 1 = 3 tokens; the spec rounds the summed characters: ceil(6 / 4) = 2.
EVIDENCE = (
    EvidenceItem(ref="payment:00000000-0000-0000-0000-000000000001", summary="e"),
    EvidenceItem(ref="aggregate:decline_code:card_declined", summary="f"),
)
EXPECTED_INPUT_TOKENS = 2
EXPECTED_OUTPUT_TOKENS = 3


@dataclass
class RecordingLlm:
    model = MODEL
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=OUTPUT_TEXT, model=MODEL)


@dataclass
class DownLlm:
    model = DOWN_MODEL

    def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmUnavailableError("provider outage")


class ModelessDownLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmUnavailableError("provider outage")


class BrokenLlm:
    model = DOWN_MODEL

    def complete(self, request: LlmRequest) -> LlmResponse:
        raise RuntimeError("programming error")


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _requests(model: str, outcome: str) -> float:
    return _sample("ops_ai_llm_requests_total", {"model": model, "outcome": outcome})


def _latency_count(model: str) -> float:
    return _sample("ops_ai_llm_latency_seconds_count", {"model": model})


def _tokens(model: str, direction: str) -> float:
    return _sample("ops_ai_llm_tokens_total", {"model": model, "direction": direction})


def _fallbacks() -> float:
    return _sample("ops_ai_llm_fallback_total")


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    memory = InMemorySpanExporter()
    configure_tracing("ops-ai").add_span_processor(SimpleSpanProcessor(memory))
    yield memory
    memory.clear()


def test_complete_returns_the_inner_response_unchanged_and_calls_the_inner_once() -> None:
    # Arrange
    inner = RecordingLlm()
    llm = InstrumentedLlm(inner)
    request = LlmRequest(question=QUESTION, evidence=EVIDENCE)

    # Act
    response = llm.complete(request)

    # Assert
    assert response == LlmResponse(text=OUTPUT_TEXT, model=MODEL)
    assert inner.calls == [request]


def test_complete_counts_an_ok_request_and_observes_the_latency_under_the_response_model() -> None:
    # Arrange
    llm = InstrumentedLlm(RecordingLlm())
    ok_before = _requests(MODEL, "ok")
    unavailable_before = _requests(MODEL, "unavailable")
    latency_before = _latency_count(MODEL)
    fallbacks_before = _fallbacks()

    # Act
    llm.complete(LlmRequest(question=QUESTION, evidence=EVIDENCE))

    # Assert
    assert _requests(MODEL, "ok") - ok_before == 1.0
    assert _requests(MODEL, "unavailable") - unavailable_before == 0.0
    assert _latency_count(MODEL) - latency_before == 1.0
    assert _fallbacks() - fallbacks_before == 0.0


def test_complete_estimates_tokens_as_ceil_of_total_chars_over_four_per_direction() -> None:
    # Arrange
    llm = InstrumentedLlm(RecordingLlm())
    input_before = _tokens(MODEL, "input")
    output_before = _tokens(MODEL, "output")

    # Act
    llm.complete(LlmRequest(question=QUESTION, evidence=EVIDENCE))

    # Assert
    assert _tokens(MODEL, "input") - input_before == EXPECTED_INPUT_TOKENS
    assert _tokens(MODEL, "output") - output_before == EXPECTED_OUTPUT_TOKENS


def test_complete_with_no_evidence_counts_only_the_question_characters() -> None:
    # Arrange
    llm = InstrumentedLlm(RecordingLlm())
    input_before = _tokens(MODEL, "input")

    # Act
    llm.complete(LlmRequest(question="abcde", evidence=()))

    # Assert
    assert _tokens(MODEL, "input") - input_before == 2.0


def test_complete_on_unavailable_counts_the_fallback_and_reraises_the_same_error() -> None:
    # Arrange
    llm = InstrumentedLlm(DownLlm())
    unavailable_before = _requests(DOWN_MODEL, "unavailable")
    ok_before = _requests(DOWN_MODEL, "ok")
    fallbacks_before = _fallbacks()

    # Act
    with pytest.raises(LlmUnavailableError) as raised:
        llm.complete(LlmRequest(question=QUESTION, evidence=EVIDENCE))

    # Assert
    assert str(raised.value) == "provider outage"
    assert _requests(DOWN_MODEL, "unavailable") - unavailable_before == 1.0
    assert _requests(DOWN_MODEL, "ok") - ok_before == 0.0
    assert _fallbacks() - fallbacks_before == 1.0


def test_complete_on_unavailable_without_a_model_attribute_labels_the_failure_unknown() -> None:
    # Arrange
    llm = InstrumentedLlm(ModelessDownLlm())
    before = _requests("unknown", "unavailable")

    # Act
    with pytest.raises(LlmUnavailableError) as raised:
        llm.complete(LlmRequest(question=QUESTION, evidence=()))

    # Assert
    assert raised.type is LlmUnavailableError
    assert _requests("unknown", "unavailable") - before == 1.0


def test_complete_does_not_count_a_fallback_for_other_errors_and_reraises_them() -> None:
    # Arrange
    llm = InstrumentedLlm(BrokenLlm())
    fallbacks_before = _fallbacks()
    unavailable_before = _requests(DOWN_MODEL, "unavailable")

    # Act
    with pytest.raises(RuntimeError) as raised:
        llm.complete(LlmRequest(question=QUESTION, evidence=()))

    # Assert
    assert str(raised.value) == "programming error"
    assert _fallbacks() - fallbacks_before == 0.0
    assert _requests(DOWN_MODEL, "unavailable") - unavailable_before == 0.0


def test_complete_runs_inside_an_llm_complete_span_that_is_a_child_of_the_current_span(
    exporter: InMemorySpanExporter,
) -> None:
    # Arrange
    tracer = configure_tracing("ops-ai").get_tracer("test")
    llm = InstrumentedLlm(RecordingLlm())

    # Act
    with tracer.start_as_current_span("outer") as outer:
        llm.complete(LlmRequest(question=QUESTION, evidence=EVIDENCE))
        outer_context = outer.get_span_context()

    # Assert
    [span] = [
        s
        for s in exporter.get_finished_spans()
        if s.name == "llm.complete" and s.context.trace_id == outer_context.trace_id
    ]
    assert span.parent is not None
    assert span.parent.span_id == outer_context.span_id


def test_a_failed_complete_still_ends_its_span_in_the_same_trace(exporter: InMemorySpanExporter) -> None:
    # Arrange
    tracer = configure_tracing("ops-ai").get_tracer("test")
    llm = InstrumentedLlm(DownLlm())

    # Act
    with tracer.start_as_current_span("outer") as outer:
        with pytest.raises(LlmUnavailableError):
            llm.complete(LlmRequest(question=QUESTION, evidence=()))
        trace_id = outer.get_span_context().trace_id

    # Assert
    names = [s.name for s in exporter.get_finished_spans() if s.context.trace_id == trace_id]
    assert sorted(names) == ["llm.complete", "outer"]


def _merchant_token(private_pem: str, merchant_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


@pytest.mark.usefixtures("redis_client", "session_factory")
def test_build_app_from_env_records_llm_metrics_for_the_default_mock_llm(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    for name in ("OPS_AI_JWT_PUBLIC_KEY_PEM", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(public_pem)
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    client = TestClient(build_app_from_env())
    headers = {"Authorization": f"Bearer {_merchant_token(private_pem, uuid.uuid4())}"}
    before = _requests("mock", "ok")

    # Act
    response = client.post("/v1/assistant/ask", json={"question": "declined payments"}, headers=headers)

    # Assert
    assert response.status_code == 200
    assert (response.json()["mode"], response.json()["model"]) == ("llm", "mock")
    assert _requests("mock", "ok") - before == 1.0
