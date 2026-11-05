"""Metric results and deterministic response-quality metrics (docs/specs/evaluation-judge.md section 1)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from ops_ai.assistant.evidence import EvidenceItem

MetricStatus = Literal["passed", "failed", "skipped", "not_applicable"]

_CITATION = re.compile(r"\[([^\s\[\]]+)\]")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


@dataclass(frozen=True)
class MetricResult:
    name: str
    score: float | None
    threshold: float
    status: MetricStatus
    detail: str = ""


def gate(name: str, score: float, threshold: float, detail: str = "") -> MetricResult:
    return MetricResult(
        name=name,
        score=score,
        threshold=threshold,
        status="passed" if score >= threshold else "failed",
        detail=detail,
    )


def skipped(name: str, threshold: float, detail: str) -> MetricResult:
    return MetricResult(name=name, score=None, threshold=threshold, status="skipped", detail=detail)


def not_applicable(name: str, threshold: float, detail: str) -> MetricResult:
    return MetricResult(name=name, score=None, threshold=threshold, status="not_applicable", detail=detail)


def cited_refs(text: str) -> tuple[str, ...]:
    return tuple(match.group(1) for match in _CITATION.finditer(text))


def faithfulness(text: str | None, evidence: Sequence[EvidenceItem], question: str = "") -> float | None:
    """Fraction of cited refs and number tokens that the evidence (or the question) supports."""
    if text is None:
        return None
    known_refs = {item.ref for item in evidence}
    known_numbers = set(_NUMBER.findall(question))
    for item in evidence:
        known_numbers.update(_NUMBER.findall(item.summary))
        known_numbers.update(_NUMBER.findall(item.ref))
    refs = cited_refs(text)
    numbers = _NUMBER.findall(_CITATION.sub(" ", text))
    claims = len(refs) + len(numbers)
    if claims == 0:
        return 1.0
    supported = sum(ref in known_refs for ref in refs) + sum(number in known_numbers for number in numbers)
    return supported / claims


def evidence_ref_coverage(refs: Sequence[str], expected_prefixes: Sequence[str]) -> float:
    if not expected_prefixes:
        return 1.0
    covered = sum(any(ref.startswith(prefix) for ref in refs) for prefix in expected_prefixes)
    return covered / len(expected_prefixes)


def answer_relevancy(
    observed: Mapping[str, object], expected: Mapping[str, object], text: str, required_terms: Sequence[str]
) -> float:
    checks = [observed.get(key) == value for key, value in expected.items()]
    lowered = text.lower()
    checks.extend(term.lower() in lowered for term in required_terms)
    if not checks:
        return 1.0
    return sum(checks) / len(checks)
