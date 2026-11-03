"""Service boundary: gateway and ops-ai talk only through `contracts` (architecture-detail §1)."""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_SRC = ROOT / "services/gateway/src"
OPS_AI_SRC = ROOT / "services/ops-ai/src"


def _imported_roots(source: str) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def _violations(src_dir: Path, forbidden_root: str) -> list[str]:
    found: list[str] = []
    for path in sorted(src_dir.rglob("*.py")):
        if forbidden_root in _imported_roots(path.read_text(encoding="utf-8")):
            found.append(str(path.relative_to(ROOT)))
    return found


def test_imported_roots_reads_plain_nested_and_from_imports() -> None:
    source = (
        "import ops_ai.x\nimport os, contracts.events\n\ndef f() -> None:\n    from gateway.db import y\n"
    )

    roots = _imported_roots(source)

    assert roots == {"ops_ai", "os", "contracts", "gateway"}


def test_imported_roots_ignores_relative_imports() -> None:
    roots = _imported_roots("from . import sibling\nfrom .models import Thing\n")

    assert roots == set()


def test_both_services_have_python_sources_to_scan() -> None:
    gateway_files = list(GATEWAY_SRC.rglob("*.py"))
    ops_ai_files = list(OPS_AI_SRC.rglob("*.py"))

    assert len(gateway_files) > 10
    assert len(ops_ai_files) > 10


def test_gateway_never_imports_ops_ai() -> None:
    violations = _violations(GATEWAY_SRC, "ops_ai")

    assert violations == []


def test_ops_ai_never_imports_gateway() -> None:
    violations = _violations(OPS_AI_SRC, "gateway")

    assert violations == []
