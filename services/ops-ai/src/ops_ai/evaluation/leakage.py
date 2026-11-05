"""Cross-tenant leakage gate (docs/specs/evaluation-judge.md section 3)."""

import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

from ops_ai.evaluation.metrics import MetricResult, gate

_TOKEN = re.compile(r"[A-Za-z0-9_-]+")
LEAKAGE_METRIC = "cross_tenant_leakage"


@dataclass(frozen=True)
class Leak:
    path: str
    token: str


def _string_leaks(text: str, path: str, foreign: AbstractSet[str]) -> list[Leak]:
    return [
        Leak(path=path, token=token)
        for token in _TOKEN.findall(text)
        if token in foreign or token.lower() in foreign
    ]


def _walk(node: object, path: str, foreign: AbstractSet[str], out: list[Leak]) -> None:
    if isinstance(node, str):
        out.extend(_string_leaks(node, path, foreign))
    elif isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}"
            if isinstance(key, str):
                out.extend(_string_leaks(key, child, foreign))
            _walk(value, child, foreign, out)
    elif isinstance(node, list | tuple):
        for index, item in enumerate(node):
            _walk(item, f"{path}[{index}]", foreign, out)


def find_leaks(payload: object, foreign: AbstractSet[str]) -> tuple[Leak, ...]:
    """Every token of every string (dict keys included) that names another tenant, in walk order."""
    if not foreign:
        return ()
    out: list[Leak] = []
    _walk(payload, "$", foreign, out)
    return tuple(out)


def _redacted_path(leak: Leak) -> str:
    """A leaking dict key is part of its own path; the report must still never carry the token."""
    return leak.path.replace(leak.token, "<redacted>")


def leakage_metric(leaks: Sequence[Leak]) -> MetricResult:
    if not leaks:
        return gate(LEAKAGE_METRIC, 1.0, 1.0)
    paths = ", ".join(dict.fromkeys(_redacted_path(leak) for leak in leaks))
    return gate(LEAKAGE_METRIC, 0.0, 1.0, f"{len(leaks)} foreign token(s) at {paths}")
