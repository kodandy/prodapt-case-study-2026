from prometheus_client import start_http_server

from ops_ai.observability.metrics import REGISTRY


def start_metrics_server(port: str | None) -> bool:
    """Serves REGISTRY on `port` for a worker process (relay, notifier); unset or empty → no server."""
    if not port:
        return False
    start_http_server(int(port), registry=REGISTRY)
    return True
