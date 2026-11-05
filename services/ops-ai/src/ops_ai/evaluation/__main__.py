"""`python -m ops_ai.evaluation show <report.json>`: prints the report, exits 1 when a gate failed."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ops_ai.evaluation.report import render_text, report_from_json


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ops_ai.evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show", help="print a report written by the eval suite")
    show.add_argument("report", type=Path)
    args = parser.parse_args(argv)
    report = report_from_json(Path(args.report).read_text(encoding="utf-8"))
    print(render_text(report))
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
