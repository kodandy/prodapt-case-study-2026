import math
import time

from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.observability.metrics import LLM_FALLBACKS, LLM_LATENCY, LLM_REQUESTS, LLM_TOKENS
from ops_ai.observability.tracing import configure_tracing

CHARS_PER_TOKEN = 4


def estimate_tokens(characters: int) -> int:
    """LlmResponse carries no provider usage, so tokens are ESTIMATED (about four characters per token)."""
    return math.ceil(characters / CHARS_PER_TOKEN)


class InstrumentedLlm:
    """LlmPort wrapper: an `llm.complete` span in the caller's trace, request/latency/token metrics, and the
    T2 fallback counter when the provider is unavailable. Prompt and answer text are never logged or
    labelled."""

    def __init__(self, inner: LlmPort) -> None:
        self._inner = inner
        self._tracer = configure_tracing("ops-ai").get_tracer("ops_ai.llm")

    @property
    def model(self) -> str:
        model = getattr(self._inner, "model", "unknown")
        return model if isinstance(model, str) else "unknown"

    def complete(self, request: LlmRequest) -> LlmResponse:
        input_chars = len(request.question) + sum(len(item.summary) for item in request.evidence)
        started = time.perf_counter()
        with self._tracer.start_as_current_span("llm.complete") as span:
            try:
                response = self._inner.complete(request)
            except LlmUnavailableError:
                model = self.model
                LLM_LATENCY.labels(model).observe(time.perf_counter() - started)
                LLM_REQUESTS.labels(model, "unavailable").inc()
                LLM_FALLBACKS.inc()
                span.set_attribute("llm.outcome", "unavailable")
                raise
            LLM_LATENCY.labels(response.model).observe(time.perf_counter() - started)
            LLM_REQUESTS.labels(response.model, "ok").inc()
            LLM_TOKENS.labels(response.model, "input").inc(estimate_tokens(input_chars))
            LLM_TOKENS.labels(response.model, "output").inc(estimate_tokens(len(response.text)))
            span.set_attribute("llm.model", response.model)
            span.set_attribute("llm.outcome", "ok")
            return response
