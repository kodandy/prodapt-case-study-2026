import threading

from gateway.payments.rails.port import RailResult


class SimulatedRail:
    """Demo rail: a destination containing "fail" is rejected, one containing "unknown" never answers
    definitively, anything else succeeds. A repeated payout id gets its first answer (rail idempotency)."""

    name = "simulated_rail"

    def __init__(self) -> None:
        self._answers: dict[str, RailResult] = {}
        self._lock = threading.Lock()

    def submit(
        self, payout_id: str, method: str, amount_minor: int, currency: str, destination: str
    ) -> RailResult:
        with self._lock:
            answer = self._answers.get(payout_id)
            if answer is None:
                answer = _answer_for(destination)
                self._answers[payout_id] = answer
            return answer


def _answer_for(destination: str) -> RailResult:
    if "fail" in destination:
        return RailResult("failed", failure_code="rail_rejected")
    if "unknown" in destination:
        return RailResult("unknown")
    return RailResult("succeeded")
