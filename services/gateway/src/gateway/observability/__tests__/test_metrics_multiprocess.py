"""With PROMETHEUS_MULTIPROC_DIR set (several uvicorn workers), /metrics reports the sum over every worker
process, not the counters of whichever worker answered the scrape."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

INCREMENT_ONE_PAYMENT_REQUEST = (
    "from gateway.observability.metrics import HTTP_REQUESTS; "
    "HTTP_REQUESTS.labels('POST', '/v1/payments', '201').inc()"
)
PRINT_METRICS = (
    "import sys; from gateway.observability.http import render_metrics; "
    "sys.stdout.write(render_metrics().decode())"
)
EXPECTED_LINE = 'gateway_http_requests_total{method="POST",route="/v1/payments",status="201"} 2.0'


def _run(code: str, multiproc_dir: Path | None) -> str:
    env = {k: v for k, v in os.environ.items() if k != "PROMETHEUS_MULTIPROC_DIR"}
    if multiproc_dir is not None:
        env["PROMETHEUS_MULTIPROC_DIR"] = str(multiproc_dir)
    return subprocess.run(  # noqa: S603 - fixed argv built from sys.executable
        [sys.executable, "-c", code], env=env, check=True, capture_output=True, text=True, timeout=60
    ).stdout


def test_render_metrics_sums_a_counter_over_two_worker_processes(tmp_path: Path) -> None:
    # Arrange
    _run(INCREMENT_ONE_PAYMENT_REQUEST, tmp_path)
    _run(INCREMENT_ONE_PAYMENT_REQUEST, tmp_path)

    # Act
    exposition = _run(PRINT_METRICS, tmp_path)

    # Assert
    assert EXPECTED_LINE in exposition.splitlines()


def test_render_metrics_without_multiproc_dir_reports_only_this_process(tmp_path: Path) -> None:
    # Arrange
    _run(INCREMENT_ONE_PAYMENT_REQUEST, tmp_path)

    # Act
    exposition = _run(PRINT_METRICS, None)

    # Assert
    assert not [line for line in exposition.splitlines() if line.startswith("gateway_http_requests_total{")]
