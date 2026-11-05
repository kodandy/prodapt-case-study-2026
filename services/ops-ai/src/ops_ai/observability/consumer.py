from typing import Protocol

from redis.exceptions import ResponseError

from ops_ai.observability.metrics import CONSUMER_LAG, CONSUMER_PENDING


class _GroupInfoReader(Protocol):
    def xinfo_groups(self, name: str) -> object: ...


def _as_int(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str | bytes) and value.strip().isdigit():
        return int(value)
    return 0


def record_consumer_lag(redis: _GroupInfoReader, stream: str, group: str) -> tuple[int, int]:
    """(lag, pending) for `group` on `stream` from XINFO GROUPS, also set on the gauges. A missing stream or
    group, or a lag Redis cannot compute (None), counts as 0."""
    lag, pending = 0, 0
    try:
        groups = redis.xinfo_groups(stream)
    except ResponseError:
        groups = []
    if isinstance(groups, list):
        for info in groups:
            if not isinstance(info, dict):
                continue
            name = info.get("name", info.get(b"name"))
            if isinstance(name, bytes):
                name = name.decode()
            if name == group:
                lag = _as_int(info.get("lag", info.get(b"lag")))
                pending = _as_int(info.get("pending", info.get(b"pending")))
                break
    CONSUMER_LAG.labels(group).set(lag)
    CONSUMER_PENDING.labels(group).set(pending)
    return lag, pending
