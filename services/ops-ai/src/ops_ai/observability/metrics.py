from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)
LLM_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0)

HTTP_REQUESTS = Counter(
    "ops_ai_http_requests",
    "HTTP requests by route template",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "ops_ai_http_request_duration_seconds",
    "HTTP request latency by route template",
    ("method", "route"),
    buckets=LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_REQUESTS = Counter(
    "ops_ai_llm_requests",
    "LLM completions by model and outcome (ok|unavailable)",
    ("model", "outcome"),
    registry=REGISTRY,
)
LLM_LATENCY = Histogram(
    "ops_ai_llm_latency_seconds",
    "LLM completion latency",
    ("model",),
    buckets=LLM_LATENCY_BUCKETS,
    registry=REGISTRY,
)
LLM_FALLBACKS = Counter(
    "ops_ai_llm_fallback",
    "LLM unavailable: the assistant answered from evidence only (T2)",
    registry=REGISTRY,
)
LLM_TOKENS = Counter(
    "ops_ai_llm_tokens",
    "ESTIMATED LLM tokens, ceil(characters / 4) per direction (input|output); not provider usage",
    ("model", "direction"),
    registry=REGISTRY,
)
EVENTS_CONSUMED = Counter(
    "ops_ai_events_consumed",
    "gw.payments.v1 entries by outcome (applied|duplicate|dead_letter)",
    ("outcome",),
    registry=REGISTRY,
)
PAYMENT_FAILURES = Counter(
    "ops_ai_payment_failures",
    "Consumed payment events not succeeded, by decline code (none when absent)",
    ("code",),
    registry=REGISTRY,
)
FRAUD_FLAGS = Counter(
    "ops_ai_fraud_flags",
    "Consumed events that raised or refreshed a fraud alert",
    ("kind",),
    registry=REGISTRY,
)
CONSUMER_LAG = Gauge(
    "ops_ai_consumer_lag", "Stream entries not yet delivered to the group", ("group",), registry=REGISTRY
)
CONSUMER_PENDING = Gauge(
    "ops_ai_consumer_pending",
    "Entries delivered to the group but not acknowledged",
    ("group",),
    registry=REGISTRY,
)
