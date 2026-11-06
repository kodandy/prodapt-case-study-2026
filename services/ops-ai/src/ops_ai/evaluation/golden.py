"""Golden cases: loading and scoring one API response (docs/specs/evaluation-judge.md section 4)."""

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort
from ops_ai.evaluation.leakage import find_leaks, leakage_metric
from ops_ai.evaluation.llm_metrics import llm_answer_relevancy, llm_faithfulness
from ops_ai.evaluation.metrics import (
    MetricResult,
    answer_relevancy,
    evidence_ref_coverage,
    faithfulness,
    gate,
    not_applicable,
)
from ops_ai.judge.hypotheses import rank_hypotheses
from ops_ai.judge.schemas import Hypothesis

Principal = Literal["sq1_merchant", "sq3_merchant", "sq4_merchant", "sq2_user", "quiet_merchant"]
DEFAULT_THRESHOLD = 1.0
DEFAULT_FAITHFULNESS_THRESHOLD = 0.9
_TOOL_CALLS_KEY = "tool_calls[].name"
_FROZEN = ConfigDict(extra="forbid", frozen=True)


class GoldenExpect(BaseModel):
    model_config = _FROZEN
    status: int
    fields: dict[str, JsonValue] = Field(default_factory=dict)
    evidence_ref_prefixes: tuple[str, ...] = ()
    required_terms: tuple[str, ...] = ()
    tool_calls: tuple[str, ...] = ()


class GoldenHypotheses(BaseModel):
    model_config = _FROZEN
    items: tuple[Hypothesis, ...]
    expected_order: tuple[str, ...]


class GoldenCase(BaseModel):
    model_config = _FROZEN
    case_id: str = Field(min_length=1)
    sample_query: Literal["SQ1", "SQ2", "SQ3", "SQ4"]
    route: str = Field(pattern=r"^/v1/")
    principal: Principal
    body: dict[str, JsonValue]
    expect: GoldenExpect
    thresholds: dict[str, float] = Field(default_factory=dict)
    hypotheses: GoldenHypotheses | None = None

    def threshold(self, metric: str) -> float:
        default = DEFAULT_FAITHFULNESS_THRESHOLD if metric == "faithfulness" else DEFAULT_THRESHOLD
        return self.thresholds.get(metric, default)

    @property
    def question(self) -> str:
        question = self.body.get("question")
        return question if isinstance(question, str) else ""


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    sample_query: str
    metrics: tuple[MetricResult, ...]

    @property
    def passed(self) -> bool:
        return all(metric.status != "failed" for metric in self.metrics)


def load_golden_cases(directory: Path) -> tuple[GoldenCase, ...]:
    """Every `*.json` file in `directory`, sorted by case_id."""
    by_id: dict[str, GoldenCase] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            case = GoldenCase.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (ValidationError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid golden case file {path.name}") from exc
        if case.case_id in by_id:
            raise ValueError(f"duplicate golden case_id {case.case_id!r} in {path.name}")
        by_id[case.case_id] = case
    return tuple(by_id[case_id] for case_id in sorted(by_id))


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _resolve(body: Mapping[str, object], dotted: str) -> object:
    node: object = body
    for part in dotted.split("."):
        if isinstance(node, Mapping):
            if part not in node:
                return _MISSING
            node = node[part]
        elif isinstance(node, list) and part.isdecimal() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def _text(body: Mapping[str, object]) -> str | None:
    text = body.get("text")
    return text if isinstance(text, str) else None


def _evidence(body: Mapping[str, object]) -> tuple[EvidenceItem, ...]:
    raw = body.get("evidence")
    if not isinstance(raw, list):
        return ()
    items: list[EvidenceItem] = []
    for entry in raw:
        if isinstance(entry, Mapping):
            ref, summary = entry.get("ref"), entry.get("summary")
            if isinstance(ref, str) and isinstance(summary, str):
                items.append(EvidenceItem(ref=ref, summary=summary))
    return tuple(items)


def _tool_call_names(body: Mapping[str, object]) -> list[object]:
    raw = body.get("tool_calls")
    if not isinstance(raw, list):
        return []
    return [entry.get("name") if isinstance(entry, Mapping) else None for entry in raw]


def _searchable_text(body: Mapping[str, object]) -> str:
    parts: list[str] = []
    for key in ("text", "summary"):
        value = body.get(key)
        if isinstance(value, str):
            parts.append(value)
    fixes = body.get("suggested_fixes")
    if isinstance(fixes, list):
        parts.extend(fix if isinstance(fix, str) else json.dumps(fix) for fix in fixes)
    return "\n".join(parts)


def _relevancy(case: GoldenCase, body: Mapping[str, object]) -> float:
    observed: dict[str, object] = {key: _resolve(body, key) for key in case.expect.fields}
    observed[_TOOL_CALLS_KEY] = _tool_call_names(body)
    expected: dict[str, object] = dict(case.expect.fields)
    expected[_TOOL_CALLS_KEY] = list(case.expect.tool_calls)
    return answer_relevancy(observed, expected, _searchable_text(body), case.expect.required_terms)


def _ranking(case_hypotheses: GoldenHypotheses, evidence: Sequence[EvidenceItem]) -> MetricResult:
    ranking = rank_hypotheses(case_hypotheses.items, evidence)
    order = tuple(item.hypothesis_id for item in ranking.ranked)
    matched = order == case_hypotheses.expected_order
    return gate(
        "hypothesis_ranking", 1.0 if matched else 0.0, 1.0, "" if matched else f"ranked {list(order)}"
    )


def score_case(
    case: GoldenCase,
    status_code: int,
    body: Mapping[str, object],
    foreign: AbstractSet[str],
    judge_llm: LlmPort | None = None,
) -> CaseResult:
    text = _text(body)
    evidence = _evidence(body)
    faithfulness_threshold = case.threshold("faithfulness")
    faithfulness_score = faithfulness(text, evidence, case.question)
    metrics: list[MetricResult] = [
        gate(
            "status",
            1.0 if status_code == case.expect.status else 0.0,
            case.threshold("status"),
            "" if status_code == case.expect.status else f"got {status_code}, expected {case.expect.status}",
        ),
        not_applicable("faithfulness", faithfulness_threshold, "no narrated text")
        if faithfulness_score is None
        else gate("faithfulness", faithfulness_score, faithfulness_threshold),
        gate("answer_relevancy", _relevancy(case, body), case.threshold("answer_relevancy")),
        gate(
            "evidence_ref_coverage",
            evidence_ref_coverage([item.ref for item in evidence], case.expect.evidence_ref_prefixes),
            case.threshold("evidence_ref_coverage"),
        ),
        leakage_metric(find_leaks(body, foreign)),
    ]
    if case.hypotheses is not None:
        metrics.append(_ranking(case.hypotheses, evidence))
    metrics.append(llm_faithfulness(judge_llm, case.question, text, evidence))
    metrics.append(llm_answer_relevancy(judge_llm, case.question, text))
    return CaseResult(case_id=case.case_id, sample_query=case.sample_query, metrics=tuple(metrics))
