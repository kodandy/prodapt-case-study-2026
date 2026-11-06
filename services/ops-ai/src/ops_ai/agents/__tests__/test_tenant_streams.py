"""Tests for the per-tenant agent streams, written from docs/specs/r2-agents.md section 4.3."""

import uuid

import pytest
from ops_ai.agents.bus import (
    DEFAULT_STREAM,
    TENANT_STREAM_MAXLEN,
    InProcessBus,
    RedisStreamTransport,
    read_tenant_messages,
    tenant_stream_key,
)
from ops_ai.agents.messages import AgentMessage, message_from_json
from ops_ai.clock import Clock
from ops_ai.scope import Scope, UserScope
from redis import Redis

from conftest import TEST_NOW

CORRELATION_ID = uuid.UUID("c0c0c0c0-0000-4000-8000-0000000000a1")
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000a2")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-0000000000a1")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
USER_SCOPE = UserScope(user_id=USER)
UNREACHABLE_REDIS_URL = "redis://127.0.0.1:1/0"


def _message(sequence: int = 1) -> AgentMessage:
    return AgentMessage(
        message_id=uuid.UUID(int=sequence),
        correlation_id=CORRELATION_ID,
        sequence=sequence,
        sender="fraud",
        recipient="compliance",
        intent="check_kyc",
        payload={"transfer_id": "t-1", "fraud_hold": False},
        evidence_refs=("transfer:t-1",),
        created_at=TEST_NOW,
    )


def _parsed(redis_client: Redis, stream: str) -> list[AgentMessage]:
    raw = redis_client.xrange(stream)
    assert isinstance(raw, list)
    parsed: list[AgentMessage] = []
    for _entry_id, fields in raw:
        assert isinstance(fields, dict)
        value = fields["message"]
        assert isinstance(value, str)
        parsed.append(message_from_json(value))
    return parsed


def _unreachable_redis() -> Redis:
    return Redis.from_url(UNREACHABLE_REDIS_URL, decode_responses=True, socket_connect_timeout=0.2)


# --- keys ------------------------------------------------------------------------------------------------


def test_a_merchant_scope_maps_to_its_own_merchant_stream_key() -> None:
    # Act
    key = tenant_stream_key(SCOPE)

    # Assert
    assert key == f"ops_ai:agent_messages:merchant:{MERCHANT}"


def test_a_user_scope_maps_to_its_own_user_stream_key() -> None:
    # Act
    key = tenant_stream_key(USER_SCOPE)

    # Assert
    assert key == f"ops_ai:agent_messages:user:{USER}"


def test_a_merchant_and_a_user_with_the_same_uuid_get_different_keys() -> None:
    # Arrange
    shared = uuid.UUID("dddddddd-0000-4000-8000-0000000000d1")

    # Act
    merchant_key = tenant_stream_key(Scope(merchant_id=shared))
    user_key = tenant_stream_key(UserScope(user_id=shared))

    # Assert
    assert merchant_key != user_key


def test_no_tenant_key_is_the_shared_stream() -> None:
    # Act
    keys = {tenant_stream_key(SCOPE), tenant_stream_key(USER_SCOPE)}

    # Assert
    assert DEFAULT_STREAM not in keys


# --- publishing --------------------------------------------------------------------------------------------


def test_a_bound_transport_reports_the_tenant_stream_key(redis_client: Redis) -> None:
    # Act
    bound = RedisStreamTransport(redis_client).for_principal(SCOPE)

    # Assert
    assert bound.stream_key == f"ops_ai:agent_messages:merchant:{MERCHANT}"


def test_a_merchant_bound_transport_writes_the_message_to_the_merchant_stream_and_the_shared_stream(
    redis_client: Redis,
) -> None:
    # Arrange
    bound = RedisStreamTransport(redis_client).for_principal(SCOPE)
    message = _message(1)

    # Act
    bound.publish(message)

    # Assert
    assert _parsed(redis_client, tenant_stream_key(SCOPE)) == [message]
    assert _parsed(redis_client, DEFAULT_STREAM) == [message]


def test_a_user_bound_transport_writes_only_to_the_user_stream_among_tenant_streams(
    redis_client: Redis,
) -> None:
    # Arrange
    bound = RedisStreamTransport(redis_client).for_principal(USER_SCOPE)
    message = _message(1)

    # Act
    bound.publish(message)

    # Assert
    assert _parsed(redis_client, tenant_stream_key(USER_SCOPE)) == [message]
    assert redis_client.xlen(tenant_stream_key(SCOPE)) == 0


def test_one_merchants_messages_never_land_in_another_merchants_stream(redis_client: Redis) -> None:
    # Arrange
    transport = RedisStreamTransport(redis_client)

    # Act
    transport.for_principal(SCOPE).publish(_message(1))

    # Assert
    assert redis_client.xlen(tenant_stream_key(SCOPE)) == 1
    assert redis_client.xlen(tenant_stream_key(OTHER_SCOPE)) == 0


def test_the_tenant_stream_is_trimmed_to_exactly_its_maxlen(redis_client: Redis) -> None:
    # Arrange
    bound = RedisStreamTransport(redis_client, tenant_maxlen=3).for_principal(SCOPE)
    messages = [_message(sequence) for sequence in range(1, 6)]

    # Act
    for message in messages:
        bound.publish(message)

    # Assert
    assert _parsed(redis_client, tenant_stream_key(SCOPE)) == messages[2:]


def test_the_default_tenant_maxlen_is_one_thousand() -> None:
    # Assert
    assert TENANT_STREAM_MAXLEN == 1_000


def test_a_bus_with_a_bound_transport_mirrors_every_sent_message_to_the_tenant_stream(
    redis_client: Redis, clock: Clock
) -> None:
    # Arrange
    bus = InProcessBus(
        CORRELATION_ID, clock, transport=RedisStreamTransport(redis_client).for_principal(USER_SCOPE)
    )

    # Act
    sent = [
        bus.send("supervisor", "fraud", "assess_transfer", {"transfer_id": "t-1"}),
        bus.send("fraud", "supervisor", "finding", {"fraud_hold": False}, ("transfer:t-1",)),
    ]

    # Assert
    assert _parsed(redis_client, tenant_stream_key(USER_SCOPE)) == sent


# --- reading -----------------------------------------------------------------------------------------------


def test_read_tenant_messages_returns_the_scope_tenants_messages_oldest_first(redis_client: Redis) -> None:
    # Arrange
    bound = RedisStreamTransport(redis_client).for_principal(SCOPE)
    messages = [_message(1), _message(2), _message(3)]
    for message in messages:
        bound.publish(message)

    # Act
    read = read_tenant_messages(redis_client, SCOPE)

    # Assert
    assert read == messages


def test_read_tenant_messages_returns_only_the_newest_count_entries(redis_client: Redis) -> None:
    # Arrange
    bound = RedisStreamTransport(redis_client).for_principal(SCOPE)
    messages = [_message(sequence) for sequence in range(1, 5)]
    for message in messages:
        bound.publish(message)

    # Act
    read = read_tenant_messages(redis_client, SCOPE, count=2)

    # Assert
    assert read == messages[2:]


def test_read_tenant_messages_never_returns_another_merchants_messages(redis_client: Redis) -> None:
    # Arrange
    RedisStreamTransport(redis_client).for_principal(SCOPE).publish(_message(1))

    # Act
    read = read_tenant_messages(redis_client, OTHER_SCOPE)

    # Assert
    assert read == []


def test_read_tenant_messages_for_a_user_never_returns_merchant_messages(redis_client: Redis) -> None:
    # Arrange
    RedisStreamTransport(redis_client).for_principal(SCOPE).publish(_message(1))

    # Act
    read = read_tenant_messages(redis_client, USER_SCOPE)

    # Assert
    assert read == []


def test_read_tenant_messages_ignores_the_shared_stream(redis_client: Redis) -> None:
    # Arrange
    RedisStreamTransport(redis_client).publish(_message(1))

    # Act
    read = read_tenant_messages(redis_client, SCOPE)

    # Assert
    assert read == []


def test_read_tenant_messages_skips_an_entry_that_does_not_parse(redis_client: Redis) -> None:
    # Arrange
    bound = RedisStreamTransport(redis_client).for_principal(SCOPE)
    bound.publish(_message(1))
    redis_client.xadd(tenant_stream_key(SCOPE), {"message": "not json"})
    bound.publish(_message(2))

    # Act
    read = read_tenant_messages(redis_client, SCOPE)

    # Assert
    assert read == [_message(1), _message(2)]


# --- best effort -------------------------------------------------------------------------------------------


def test_a_bound_transport_swallows_a_redis_outage() -> None:
    # Arrange
    bound = RedisStreamTransport(_unreachable_redis()).for_principal(SCOPE)

    # Act
    bound.publish(_message(1))

    # Assert
    assert bound.stream_key == tenant_stream_key(SCOPE)


def test_a_bus_with_an_unreachable_bound_transport_still_queues_and_records_history(clock: Clock) -> None:
    # Arrange
    bus = InProcessBus(
        CORRELATION_ID, clock, transport=RedisStreamTransport(_unreachable_redis()).for_principal(SCOPE)
    )

    # Act
    sent = bus.send("supervisor", "fraud", "investigate", {})

    # Assert
    assert bus.history == (sent,)
    assert bus.next_message() == sent


def test_read_tenant_messages_returns_an_empty_list_on_a_redis_outage() -> None:
    # Act
    read = read_tenant_messages(_unreachable_redis(), SCOPE)

    # Assert
    assert read == []


@pytest.mark.parametrize("count", [0, -1])
def test_read_tenant_messages_with_a_non_positive_count_returns_an_empty_list(
    redis_client: Redis, count: int
) -> None:
    # Arrange
    RedisStreamTransport(redis_client).for_principal(SCOPE).publish(_message(1))

    # Act
    read = read_tenant_messages(redis_client, SCOPE, count=count)

    # Assert
    assert read == []
