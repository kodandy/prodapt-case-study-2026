from collections.abc import Callable
from datetime import UTC, datetime, timedelta

Clock = Callable[[], datetime]


def system_clock() -> datetime:
    return datetime.now(UTC)


class FixedClock:
    """Injectable clock for tests and DEMO_NOW."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def __call__(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta
