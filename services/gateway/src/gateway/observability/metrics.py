from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

# Buckets bracket the NFR target (P99 < 2 s) so the alert and the load test read the same histogram.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "gateway_http_requests",
    "HTTP requests by route template",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "gateway_http_request_duration_seconds",
    "HTTP request latency by route template",
    ("method", "route"),
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
PAYMENT_EVENTS = Counter(
    "gateway_payment_events",
    "Payment state changes written to the outbox",
    ("method", "state", "decline_code"),
    registry=REGISTRY,
)
PROVIDER_CALLS = Counter(
    "gateway_provider_calls",
    "Acquirer calls by route and outcome (succeeded|declined|timeout|error|short_circuited)",
    ("route", "outcome"),
    registry=REGISTRY,
)
CIRCUIT_STATE = Gauge(
    "gateway_circuit_state", "Provider circuit: 0 closed, 1 open, 2 half_open", ("route",), registry=REGISTRY
)
RATE_LIMITED = Counter(
    "gateway_rate_limit_rejected",
    "Requests rejected with 429 by the token bucket",
    ("scope",),
    registry=REGISTRY,
)
RATE_LIMIT_FAIL_OPEN = Counter(
    "gateway_rate_limit_fail_open",
    "Rate-limit checks let through because Redis failed",
    ("scope",),
    registry=REGISTRY,
)
OUTBOX_PENDING = Gauge("gateway_outbox_pending", "Unpublished outbox rows", registry=REGISTRY)
OUTBOX_OLDEST_AGE = Gauge(
    "gateway_outbox_oldest_pending_age_seconds", "Age of the oldest unpublished outbox row", registry=REGISTRY
)
OUTBOX_PUBLISHED = Counter("gateway_outbox_published", "Outbox rows published to streams", registry=REGISTRY)
