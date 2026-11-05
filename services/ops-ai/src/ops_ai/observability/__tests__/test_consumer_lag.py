"""Tests for ops_ai.observability.consumer.record_consumer_lag, from the NFR spec C (consumer.py)."""

import uuid
from dataclasses import dataclass
from typing import cast

from ops_ai.observability.consumer import record_consumer_lag
from ops_ai.observability.metrics import REGISTRY
from redis import Redis

STREAM = "gw.payments.v1"
GROUP = "lag-test-group"


@dataclass
class StubRedis:
    """Answers XINFO GROUPS with a fixed reply (redis-py's decoded shape: a list of dicts)."""

    groups: list[dict[str, object]]

    def xinfo_groups(self, name: str) -> list[dict[str, object]]:
        return self.groups


def _gauge(name: str, group: str) -> float | None:
    return REGISTRY.get_sample_value(name, {"group": group})


def _add_entries(redis: Redis, count: int) -> None:
    for _ in range(count):
        redis.xadd(STREAM, {"event_id": str(uuid.uuid4()), "payload": "{}"})


def test_record_consumer_lag_for_a_missing_stream_returns_zero_zero_and_sets_the_gauges(
    redis_client: Redis,
) -> None:
    # Arrange
    group = "lag-test-missing-stream"

    # Act
    result = record_consumer_lag(redis_client, "no.such.stream.v1", group)

    # Assert
    assert result == (0, 0)
    assert _gauge("ops_ai_consumer_lag", group) == 0.0
    assert _gauge("ops_ai_consumer_pending", group) == 0.0


def test_record_consumer_lag_for_a_missing_group_returns_zero_zero(redis_client: Redis) -> None:
    # Arrange
    _add_entries(redis_client, 2)

    # Act
    result = record_consumer_lag(redis_client, STREAM, "lag-test-missing-group")

    # Assert
    assert result == (0, 0)
    assert _gauge("ops_ai_consumer_lag", "lag-test-missing-group") == 0.0


def test_record_consumer_lag_counts_undelivered_entries_as_lag_and_unacked_ones_as_pending(
    redis_client: Redis,
) -> None:
    # Arrange
    _add_entries(redis_client, 5)
    redis_client.xgroup_create(STREAM, GROUP, id="0")
    redis_client.xreadgroup(GROUP, "consumer-1", {STREAM: ">"}, count=2)

    # Act
    result = record_consumer_lag(redis_client, STREAM, GROUP)

    # Assert
    assert result == (3, 2)
    assert _gauge("ops_ai_consumer_lag", GROUP) == 3.0
    assert _gauge("ops_ai_consumer_pending", GROUP) == 2.0


def test_record_consumer_lag_drops_pending_to_zero_once_the_entries_are_acknowledged(
    redis_client: Redis,
) -> None:
    # Arrange
    _add_entries(redis_client, 2)
    redis_client.xgroup_create(STREAM, GROUP, id="0")
    delivered = cast(
        list[tuple[str, list[tuple[str, dict[str, str]]]]],
        redis_client.xreadgroup(GROUP, "consumer-1", {STREAM: ">"}, count=10),
    )
    entry_ids = [entry_id for _stream, entries in delivered for entry_id, _fields in entries]
    redis_client.xack(STREAM, GROUP, *entry_ids)

    # Act
    result = record_consumer_lag(redis_client, STREAM, GROUP)

    # Assert
    assert result == (0, 0)
    assert _gauge("ops_ai_consumer_pending", GROUP) == 0.0


def test_record_consumer_lag_treats_an_unknown_lag_as_zero() -> None:
    # Arrange
    group = "lag-test-unknown-lag"
    redis = StubRedis([{"name": group, "pending": 4, "lag": None}])

    # Act
    result = record_consumer_lag(redis, STREAM, group)

    # Assert
    assert result == (0, 4)
    assert _gauge("ops_ai_consumer_lag", group) == 0.0
    assert _gauge("ops_ai_consumer_pending", group) == 4.0


def test_record_consumer_lag_reads_only_the_requested_group() -> None:
    # Arrange
    redis = StubRedis(
        [
            {"name": "lag-test-other", "pending": 9, "lag": 9},
            {"name": "lag-test-wanted", "pending": 1, "lag": 7},
        ]
    )

    # Act
    result = record_consumer_lag(redis, STREAM, "lag-test-wanted")

    # Assert
    assert result == (7, 1)
    assert _gauge("ops_ai_consumer_lag", "lag-test-wanted") == 7.0
    assert _gauge("ops_ai_consumer_pending", "lag-test-wanted") == 1.0
    assert _gauge("ops_ai_consumer_lag", "lag-test-other") is None
