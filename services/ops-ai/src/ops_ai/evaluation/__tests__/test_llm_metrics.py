"""LLM-judged response-quality metrics (docs/specs/evaluation-judge.md section 4.1)."""

from collections.abc import Callable, Sequence

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.assistant.openai_llm import OpenAiLlm
from ops_ai.evaluation.llm_metrics import judge_llm_from_env, llm_answer_relevancy, llm_faithfulness
from ops_ai.evaluation.metrics import MetricResult

QUESTION = "Why are 15% of my card payments failing?"
ANSWER = "BIN 400000 is down at the issuer [aggregate:bin:400000]."
EVIDENCE = [
    EvidenceItem(ref="aggregate:bin:400000", summary="BIN 400000: 32 of 40 payments failed"),
    EvidenceItem(ref="incident:inc_0001", summary="Issuer outage on BIN 400000"),
]
GOOD_REPLY = '{"score": 0.8, "reason": "ok"}'

Caller = Callable[[LlmPort | None, str | None, float], MetricResult]


class _StubLlm:
    def __init__(self, reply: str = GOOD_REPLY, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return LlmResponse(text=self.reply, model="stub-judge")


def _faithfulness(llm: LlmPort | None, text: str | None, threshold: float = 0.7) -> MetricResult:
    return llm_faithfulness(llm, QUESTION, text, EVIDENCE, threshold)


def _relevancy(llm: LlmPort | None, text: str | None, threshold: float = 0.7) -> MetricResult:
    return llm_answer_relevancy(llm, QUESTION, text, threshold)


BOTH: Sequence[tuple[str, Caller]] = (
    ("llm_faithfulness", _faithfulness),
    ("llm_answer_relevancy", _relevancy),
)
both_metrics = pytest.mark.parametrize(("name", "call"), BOTH, ids=[name for name, _ in BOTH])


@both_metrics
def test_metric_is_skipped_when_there_is_no_llm(name: str, call: Caller) -> None:
    # Arrange / Act
    result = call(None, ANSWER, 0.7)

    # Assert
    assert (result.name, result.score, result.threshold, result.status) == (name, None, 0.7, "skipped")
    assert "no OPENAI_API_KEY" in result.detail


@both_metrics
def test_metric_is_not_applicable_without_a_text_and_makes_no_call(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm()

    # Act
    result = call(llm, None, 0.7)

    # Assert
    observed = (result.name, result.score, result.threshold, result.status)
    assert observed == (name, None, 0.7, "not_applicable")
    assert llm.requests == []


@both_metrics
def test_metric_passes_with_the_score_of_the_judge_reply(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm('{"score": 0.8, "reason": "grounded in the evidence"}')

    # Act
    result = call(llm, ANSWER, 0.7)

    # Assert
    assert (result.name, result.score, result.threshold, result.status) == (name, 0.8, 0.7, "passed")


@both_metrics
def test_metric_accepts_a_reply_wrapped_in_a_json_fence(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm('```json\n{"score": 0.9, "reason": "ok"}\n```')

    # Act
    result = call(llm, ANSWER, 0.7)

    # Assert
    assert (result.score, result.status) == (0.9, "passed")


@both_metrics
def test_metric_fails_when_the_score_is_below_the_threshold(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm('{"score": 0.5, "reason": "partly unsupported"}')

    # Act
    result = call(llm, ANSWER, 0.7)

    # Assert
    assert (result.name, result.score, result.threshold, result.status) == (name, 0.5, 0.7, "failed")


@both_metrics
def test_metric_passes_when_the_score_equals_the_threshold(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm('{"score": 0.75, "reason": "ok"}')

    # Act
    result = call(llm, ANSWER, 0.75)

    # Assert
    assert (result.score, result.threshold, result.status) == (0.75, 0.75, "passed")


@both_metrics
def test_metric_uses_the_given_threshold(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm('{"score": 0.8, "reason": "ok"}')

    # Act
    result = call(llm, ANSWER, 0.9)

    # Assert
    assert (result.score, result.threshold, result.status) == (0.8, 0.9, "failed")


@both_metrics
@pytest.mark.parametrize(
    "reply",
    [
        '{"score": 0.8, "reason": "ok", "extra": 1}',
        '{"score": 1.5, "reason": "ok"}',
        '{"score": -0.1, "reason": "ok"}',
        '{"reason": "no score"}',
        '{"score": 0.8, "reason": "' + "x" * 301 + '"}',
        "not json at all",
        "",
        "[0.8]",
        '{"score": 0.9,' + " " * 9000 + '"reason": "ok"}',
    ],
    ids=[
        "extra-key",
        "score-above-one",
        "score-below-zero",
        "missing-score",
        "reason-too-long",
        "not-json",
        "empty",
        "json-list",
        "over-8-kib",
    ],
)
def test_metric_fails_with_judge_reply_rejected_on_an_invalid_reply(
    name: str, call: Caller, reply: str
) -> None:
    # Arrange
    llm = _StubLlm(reply)

    # Act
    result = call(llm, ANSWER, 0.7)

    # Assert
    assert (result.name, result.status) == (name, "failed")
    assert "judge reply rejected" in result.detail


@both_metrics
def test_metric_is_skipped_with_judge_unavailable_when_the_llm_raises(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm(error=LlmUnavailableError("budget exhausted"))

    # Act
    result = call(llm, ANSWER, 0.7)

    # Assert
    assert (result.name, result.score, result.threshold, result.status) == (name, None, 0.7, "skipped")
    assert "judge unavailable" in result.detail


@both_metrics
def test_metric_makes_exactly_one_call_with_the_answer_and_the_question(name: str, call: Caller) -> None:
    # Arrange
    llm = _StubLlm()

    # Act
    call(llm, ANSWER, 0.7)

    # Assert
    assert len(llm.requests) == 1
    assert ANSWER in llm.requests[0].question
    assert QUESTION in llm.requests[0].question


def test_llm_faithfulness_passes_the_evidence_through_to_the_judge() -> None:
    # Arrange
    llm = _StubLlm()

    # Act
    llm_faithfulness(llm, QUESTION, ANSWER, EVIDENCE)

    # Assert
    assert llm.requests[0].evidence == tuple(EVIDENCE)


def test_llm_faithfulness_defaults_the_threshold_to_point_seven() -> None:
    # Arrange
    llm = _StubLlm('{"score": 0.7, "reason": "ok"}')

    # Act
    result = llm_faithfulness(llm, QUESTION, ANSWER, EVIDENCE)

    # Assert
    assert (result.score, result.threshold, result.status) == (0.7, 0.7, "passed")


def test_llm_answer_relevancy_defaults_the_threshold_to_point_seven() -> None:
    # Arrange
    llm = _StubLlm('{"score": 0.69, "reason": "ok"}')

    # Act
    result = llm_answer_relevancy(llm, QUESTION, ANSWER)

    # Assert
    assert (result.score, result.threshold, result.status) == (0.69, 0.7, "failed")


def test_judge_llm_from_env_is_none_without_an_api_key() -> None:
    # Arrange / Act
    llm = judge_llm_from_env({})

    # Assert
    assert llm is None


def test_judge_llm_from_env_is_none_for_an_empty_api_key() -> None:
    # Arrange / Act
    llm = judge_llm_from_env({"OPENAI_API_KEY": ""})

    # Assert
    assert llm is None


def test_judge_llm_from_env_builds_an_openai_llm_for_a_non_empty_api_key() -> None:
    # Arrange / Act
    llm = judge_llm_from_env({"OPENAI_API_KEY": "sk-test"})

    # Assert
    assert isinstance(llm, OpenAiLlm)
