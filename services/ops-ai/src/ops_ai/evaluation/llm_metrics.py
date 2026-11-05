"""LLM-judged response-quality metrics (docs/specs/evaluation-judge.md section 4.1).

Run only when a judge LLM is configured (OPENAI_API_KEY); otherwise they report `skipped`.
"""

import os
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.openai_llm import DEFAULT_CHAT_MODEL, DEFAULT_FALLBACK_MODEL, OpenAiLlm
from ops_ai.evaluation.metrics import MetricResult, gate, not_applicable, skipped
from ops_ai.judge.llm_parse import parse_reply

LLM_FAITHFULNESS = "llm_faithfulness"
LLM_ANSWER_RELEVANCY = "llm_answer_relevancy"
DEFAULT_LLM_THRESHOLD = 0.7
NO_JUDGE = "no OPENAI_API_KEY"
NO_TEXT = "no narrated text"
REJECTED = "judge reply rejected"
UNAVAILABLE = "judge unavailable"

FAITHFULNESS_RUBRIC = (
    "You are an evaluation judge. Score how faithful the ANSWER is to the EVIDENCE items: 1.0 when every "
    "claim (numbers, causes, references) is supported by the evidence, 0.0 when none is. Ignore style."
)
RELEVANCY_RUBRIC = (
    "You are an evaluation judge. Score how directly the ANSWER addresses the QUESTION: 1.0 when it answers "
    "it (cause and next step when asked), 0.0 when it is off-topic. Ignore style."
)
REPLY_FORMAT = 'Reply with JSON only: {"score": <number 0..1>, "reason": "<at most 300 characters>"}.'


class _JudgeScore(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    score: float = Field(ge=0.0, le=1.0)
    reason: str = Field(max_length=300)


def _judge(
    name: str, llm: LlmPort | None, prompt: str, evidence: Sequence[EvidenceItem], threshold: float
) -> MetricResult:
    if llm is None:
        return skipped(name, threshold, NO_JUDGE)
    try:
        response = llm.complete(LlmRequest(question=prompt, evidence=tuple(evidence)))
    except LlmUnavailableError:
        return skipped(name, threshold, UNAVAILABLE)
    reply = parse_reply(response.text, _JudgeScore)
    if reply is None:
        return MetricResult(name=name, score=0.0, threshold=threshold, status="failed", detail=REJECTED)
    return gate(name, reply.score, threshold, reply.reason)


def llm_faithfulness(
    llm: LlmPort | None,
    question: str,
    text: str | None,
    evidence: Sequence[EvidenceItem],
    threshold: float = DEFAULT_LLM_THRESHOLD,
) -> MetricResult:
    if llm is not None and text is None:
        return not_applicable(LLM_FAITHFULNESS, threshold, NO_TEXT)
    prompt = f"{FAITHFULNESS_RUBRIC}\n{REPLY_FORMAT}\nQUESTION: {question}\nANSWER: {text}"
    return _judge(LLM_FAITHFULNESS, llm, prompt, evidence, threshold)


def llm_answer_relevancy(
    llm: LlmPort | None, question: str, text: str | None, threshold: float = DEFAULT_LLM_THRESHOLD
) -> MetricResult:
    if llm is not None and text is None:
        return not_applicable(LLM_ANSWER_RELEVANCY, threshold, NO_TEXT)
    prompt = f"{RELEVANCY_RUBRIC}\n{REPLY_FORMAT}\nQUESTION: {question}\nANSWER: {text}"
    return _judge(LLM_ANSWER_RELEVANCY, llm, prompt, (), threshold)


def judge_llm_from_env(environ: Mapping[str, str] = os.environ) -> LlmPort | None:
    """The real judge only when OPENAI_API_KEY is set; the key never leaves the SecretStr."""
    api_key = environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return None
    return OpenAiLlm(
        SecretStr(api_key),
        chat_model=environ.get("OPENAI_CHAT_MODEL") or DEFAULT_CHAT_MODEL,
        fallback_model=environ.get("OPENAI_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL,
    )
