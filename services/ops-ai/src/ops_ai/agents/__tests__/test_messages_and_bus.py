"""Tests for agent messages, the in-process bus, its transports and persistence, written from
docs/specs/r2-agents.md section 4 (`messages.py`, `bus.py`)."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from gateway.clock import FixedClock
from ops_ai.agents.bus import (
    MAX_MESSAGES,
    InProcessBus,
    MessageBudgetExceededError,
    RedisStreamTransport,
    load_messages,
    persist_messages,
)
from ops_ai.agents.messages import AgentMessage, JsonScalar, message_from_json, message_to_json
from ops_ai.scope import Scope
from redis import Redis
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

CORRELATION_ID = uuid.UUID("c0c0c0c0-0000-4000-8000-000000000001")
OTHER_CORRELATION_ID = uuid.UUID("c0c0c0c0-0000-4000-8000-000000000002")
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
DEFAULT_STREAM = "ops_ai:agent_messages"

SELECT_ROWS = text(
    "SELECT message_id, correlation_id, sequence, sender, recipient, intent, payload, evidence_refs, "
    "merchant_id, user_id, created_at FROM ops_ai.agent_messages WHERE correlation_id = :cid "
    "ORDER BY sequence"
)


@dataclass
class RecordingTransport:
    published: list[AgentMessage] = field(default_factory=list)

    def publish(self, message: AgentMessage) -> None:
        self.published.append(message)


def _message(sequence: int = 1, payload: dict[str, JsonScalar] | None = None) -> AgentMessage:
    return AgentMessage(
        message_id=uuid.UUID(int=sequence),
        correlation_id=CORRELATION_ID,
        sequence=sequence,
        sender="fraud",
        recipient="compliance",
        intent="check_kyc",
        payload={"transfer_id": "t-1", "fraud_hold": False} if payload is None else payload,
        evidence_refs=("transfer:t-1", "episode:p-1"),
        created_at=TEST_NOW,
    )


def _three_message_bus(clock: FixedClock, correlation_id: uuid.UUID = CORRELATION_ID) -> InProcessBus:
    bus = InProcessBus(correlation_id, clock)
    bus.send("supervisor", "fraud", "assess_transfer", {"transfer_id": "t-1"})
    bus.send(
        "fraud", "compliance", "check_kyc", {"transfer_id": "t-1", "fraud_hold": False}, ("transfer:t-1",)
    )
    bus.send("fraud", "supervisor", "finding", {"fraud_hold": False})
    return bus


# --- json round trip -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"transfer_id": "t-1", "fraud_hold": False},
        {"count": 12, "share": 0.25, "note": None, "label": "ü日本"},
        {"balance_covered_amount": True, "kyc_tier": 1, "period": "2026-10"},
    ],
    ids=["empty", "transfer", "mixed_scalars", "bool_int_str"],
)
def test_message_json_round_trip_gives_an_equal_message(payload: dict[str, JsonScalar]) -> None:
    # Arrange
    message = _message(3, payload)

    # Act
    parsed = message_from_json(message_to_json(message))

    # Assert
    assert parsed == message


def test_message_json_round_trip_keeps_booleans_booleans_and_evidence_refs_a_tuple() -> None:
    # Arrange
    message = _message(1, {"flag": True, "n": 1})

    # Act
    parsed = message_from_json(message_to_json(message))

    # Assert
    assert type(parsed.payload["flag"]) is bool
    assert type(parsed.payload["n"]) is int
    assert parsed.evidence_refs == ("transfer:t-1", "episode:p-1")
    assert isinstance(parsed.evidence_refs, tuple)


def test_message_json_round_trip_keeps_the_timestamp_instant_and_timezone_awareness() -> None:
    # Arrange
    at = datetime(2026, 11, 3, 12, 0, 5, 123456, tzinfo=UTC)
    message = AgentMessage(
        message_id=uuid.UUID(int=9),
        correlation_id=CORRELATION_ID,
        sequence=9,
        sender="settlement",
        recipient="compliance",
        intent="funds_verified",
        payload={"balance_covered_amount": True},
        evidence_refs=(),
        created_at=at,
    )

    # Act
    parsed = message_from_json(message_to_json(message))

    # Assert
    assert parsed.created_at == at
    assert parsed.created_at.utcoffset() == timedelta(0)


# --- the bus: sequence, FIFO, history --------------------------------------------------------------------


def test_send_numbers_messages_from_one_and_fills_every_field(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)

    # Act
    first = bus.send("supervisor", "fraud", "assess_transfer", {"transfer_id": "t-1"})
    second = bus.send("fraud", "compliance", "check_kyc", {"fraud_hold": False}, ("transfer:t-1",))

    # Assert
    assert (first.sequence, second.sequence) == (1, 2)
    assert (first.sender, first.recipient, first.intent) == ("supervisor", "fraud", "assess_transfer")
    assert dict(first.payload) == {"transfer_id": "t-1"}
    assert first.evidence_refs == ()
    assert first.correlation_id == CORRELATION_ID
    assert first.created_at == TEST_NOW
    assert (second.sender, second.recipient, second.intent) == ("fraud", "compliance", "check_kyc")
    assert dict(second.payload) == {"fraud_hold": False}
    assert second.evidence_refs == ("transfer:t-1",)
    assert second.created_at == TEST_NOW
    assert isinstance(first.message_id, uuid.UUID)
    assert first.message_id != second.message_id


def test_send_stamps_created_at_from_the_injected_clock_at_send_time(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)
    first = bus.send("supervisor", "fraud", "investigate", {})
    clock.advance(timedelta(seconds=30))

    # Act
    second = bus.send("fraud", "supervisor", "finding", {})

    # Assert
    assert first.created_at == TEST_NOW
    assert second.created_at == TEST_NOW + timedelta(seconds=30)


def test_next_message_delivers_in_fifo_order_then_none(clock: FixedClock) -> None:
    # Arrange
    bus = _three_message_bus(clock)

    # Act
    delivered = [bus.next_message(), bus.next_message(), bus.next_message()]
    after_empty = bus.next_message()

    # Assert
    assert [message.sequence for message in delivered if message is not None] == [1, 2, 3]
    assert [message.intent for message in delivered if message is not None] == [
        "assess_transfer",
        "check_kyc",
        "finding",
    ]
    assert after_empty is None


def test_next_message_on_a_fresh_bus_is_none(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)

    # Act
    message = bus.next_message()

    # Assert
    assert message is None


def test_next_message_interleaves_with_new_sends_in_fifo_order(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)
    bus.send("supervisor", "fraud", "investigate", {})

    # Act
    first = bus.next_message()
    bus.send("fraud", "dispute", "check_disputes", {})
    bus.send("fraud", "supervisor", "finding", {})
    second = bus.next_message()
    third = bus.next_message()

    # Assert
    assert first is not None
    assert second is not None
    assert third is not None
    assert [first.sequence, second.sequence, third.sequence] == [1, 2, 3]
    assert bus.next_message() is None


def test_history_lists_every_sent_message_in_order_and_survives_delivery(clock: FixedClock) -> None:
    # Arrange
    bus = _three_message_bus(clock)
    while bus.next_message() is not None:
        pass

    # Act
    history = bus.history

    # Assert
    assert isinstance(history, tuple)
    assert [message.sequence for message in history] == [1, 2, 3]
    assert [(m.sender, m.recipient, m.intent) for m in history] == [
        ("supervisor", "fraud", "assess_transfer"),
        ("fraud", "compliance", "check_kyc"),
        ("fraud", "supervisor", "finding"),
    ]


def test_history_of_a_fresh_bus_is_empty(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)

    # Act
    history = bus.history

    # Assert
    assert history == ()


def test_history_returns_the_same_messages_that_send_returned(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)

    # Act
    sent = [
        bus.send("supervisor", "fraud", "investigate", {}),
        bus.send("fraud", "supervisor", "finding", {}),
    ]

    # Assert
    assert list(bus.history) == sent


# --- the message budget ----------------------------------------------------------------------------------


def test_the_default_message_budget_is_sixteen() -> None:
    # Arrange / Act
    budget = MAX_MESSAGES

    # Assert
    assert budget == 16


def test_the_seventeenth_send_raises_message_budget_exceeded_and_is_not_queued(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)
    for _ in range(16):
        bus.send("supervisor", "fraud", "investigate", {})

    # Act
    with pytest.raises(MessageBudgetExceededError):
        bus.send("supervisor", "fraud", "investigate", {})

    # Assert
    assert len(bus.history) == 16
    assert [message.sequence for message in bus.history] == list(range(1, 17))
    delivered = 0
    while bus.next_message() is not None:
        delivered += 1
    assert delivered == 16


def test_the_sixteenth_send_is_still_accepted(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)
    for _ in range(15):
        bus.send("supervisor", "fraud", "investigate", {})

    # Act
    last = bus.send("supervisor", "fraud", "investigate", {})

    # Assert
    assert last.sequence == 16


def test_a_custom_budget_of_two_rejects_the_third_send(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock, max_messages=2)
    bus.send("supervisor", "fraud", "investigate", {})
    bus.send("fraud", "supervisor", "finding", {})

    # Act
    with pytest.raises(MessageBudgetExceededError):
        bus.send("supervisor", "fraud", "investigate", {})

    # Assert
    assert len(bus.history) == 2


def test_the_budget_stays_exceeded_for_every_later_send(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock, max_messages=1)
    bus.send("supervisor", "fraud", "investigate", {})

    # Act
    with pytest.raises(MessageBudgetExceededError):
        bus.send("fraud", "supervisor", "finding", {})
    with pytest.raises(MessageBudgetExceededError):
        bus.send("fraud", "supervisor", "finding", {})

    # Assert
    assert len(bus.history) == 1


def test_delivering_messages_does_not_free_budget(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock, max_messages=2)
    bus.send("supervisor", "fraud", "investigate", {})
    bus.send("fraud", "supervisor", "finding", {})
    bus.next_message()
    bus.next_message()

    # Act
    with pytest.raises(MessageBudgetExceededError):
        bus.send("supervisor", "fraud", "investigate", {})

    # Assert
    assert len(bus.history) == 2


# --- transports ------------------------------------------------------------------------------------------


def test_a_recording_transport_receives_each_message_in_send_order(clock: FixedClock) -> None:
    # Arrange
    transport = RecordingTransport()
    bus = InProcessBus(CORRELATION_ID, clock, transport=transport)

    # Act
    sent = [
        bus.send("supervisor", "fraud", "assess_transfer", {"transfer_id": "t-1"}),
        bus.send("fraud", "compliance", "check_kyc", {"fraud_hold": False}),
        bus.send("fraud", "supervisor", "finding", {"fraud_hold": False}),
    ]

    # Assert
    assert transport.published == sent


def test_a_message_over_the_budget_is_not_published(clock: FixedClock) -> None:
    # Arrange
    transport = RecordingTransport()
    bus = InProcessBus(CORRELATION_ID, clock, max_messages=1, transport=transport)
    first = bus.send("supervisor", "fraud", "investigate", {})

    # Act
    with pytest.raises(MessageBudgetExceededError):
        bus.send("fraud", "supervisor", "finding", {})

    # Assert
    assert transport.published == [first]


def test_a_bus_without_a_transport_still_queues_and_records_history(clock: FixedClock) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock)

    # Act
    sent = bus.send("supervisor", "fraud", "investigate", {})

    # Assert
    assert bus.next_message() == sent
    assert bus.history == (sent,)


def test_redis_stream_transport_writes_a_message_field_that_parses_back_equal(
    redis_client: Redis,
) -> None:
    # Arrange
    transport = RedisStreamTransport(redis_client)
    message = _message(1)

    # Act
    transport.publish(message)

    # Assert
    entries = redis_client.xrange(DEFAULT_STREAM)
    assert len(entries) == 1
    fields = entries[0][1]
    assert set(fields) == {"message"}
    assert message_from_json(fields["message"]) == message


def test_redis_stream_transport_appends_messages_in_publish_order(redis_client: Redis) -> None:
    # Arrange
    transport = RedisStreamTransport(redis_client)
    messages = [_message(1), _message(2, {"fraud_hold": True}), _message(3, {})]

    # Act
    for message in messages:
        transport.publish(message)

    # Assert
    entries = redis_client.xrange(DEFAULT_STREAM)
    assert [message_from_json(fields["message"]) for _id, fields in entries] == messages


def test_redis_stream_transport_uses_the_stream_name_it_is_given(redis_client: Redis) -> None:
    # Arrange
    transport = RedisStreamTransport(redis_client, stream="custom:agent_stream", maxlen=100)
    message = _message(1)

    # Act
    transport.publish(message)

    # Assert
    assert redis_client.xlen("custom:agent_stream") == 1
    assert redis_client.xlen(DEFAULT_STREAM) == 0


def test_a_bus_with_a_redis_transport_writes_every_sent_message_to_the_stream(
    redis_client: Redis, clock: FixedClock
) -> None:
    # Arrange
    bus = InProcessBus(CORRELATION_ID, clock, transport=RedisStreamTransport(redis_client))

    # Act
    sent = [
        bus.send("supervisor", "fraud", "assess_transfer", {"transfer_id": "t-1"}),
        bus.send("fraud", "supervisor", "finding", {"fraud_hold": False}, ("transfer:t-1",)),
    ]

    # Assert
    entries = redis_client.xrange(DEFAULT_STREAM)
    assert [message_from_json(fields["message"]) for _id, fields in entries] == sent


# --- persistence -----------------------------------------------------------------------------------------


def test_persist_messages_stores_every_column_with_the_merchant_and_returns_the_count(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bus = _three_message_bus(clock)

    # Act
    with session_factory() as session:
        stored = persist_messages(session, list(bus.history), MERCHANT, None)
        session.commit()

    # Assert
    assert stored == 3
    with session_factory() as session:
        rows = [dict(row) for row in session.execute(SELECT_ROWS, {"cid": CORRELATION_ID}).mappings()]
    assert rows == [
        {
            "message_id": message.message_id,
            "correlation_id": CORRELATION_ID,
            "sequence": message.sequence,
            "sender": message.sender,
            "recipient": message.recipient,
            "intent": message.intent,
            "payload": dict(message.payload),
            "evidence_refs": list(message.evidence_refs),
            "merchant_id": MERCHANT,
            "user_id": None,
            "created_at": TEST_NOW,
        }
        for message in bus.history
    ]


def test_persist_messages_stores_the_user_id_for_a_user_scoped_trail(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bus = _three_message_bus(clock)

    # Act
    with session_factory() as session:
        persist_messages(session, list(bus.history), None, USER)
        session.commit()

    # Assert
    with session_factory() as session:
        rows = session.execute(SELECT_ROWS, {"cid": CORRELATION_ID}).mappings().all()
    assert [(row["merchant_id"], row["user_id"]) for row in rows] == [(None, USER)] * 3


def test_persist_messages_with_no_messages_returns_zero(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    messages: list[AgentMessage] = []

    # Act
    with session_factory() as session:
        stored = persist_messages(session, messages, MERCHANT, None)
        session.commit()

    # Assert
    assert stored == 0
    with session_factory() as session:
        assert session.execute(text("SELECT count(*) FROM ops_ai.agent_messages")).scalar_one() == 0


def test_load_messages_returns_the_scope_merchants_messages_ordered_by_sequence(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bus = _three_message_bus(clock)
    with session_factory() as session:
        persist_messages(session, list(reversed(bus.history)), MERCHANT, None)
        session.commit()

    # Act
    with session_factory() as session:
        loaded = load_messages(session, SCOPE, CORRELATION_ID)

    # Assert
    assert loaded == list(bus.history)
    assert [message.sequence for message in loaded] == [1, 2, 3]


def test_load_messages_returns_an_empty_list_for_another_merchants_scope(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bus = _three_message_bus(clock)
    with session_factory() as session:
        persist_messages(session, list(bus.history), MERCHANT, None)
        session.commit()

    # Act
    with session_factory() as session:
        loaded = load_messages(session, OTHER_SCOPE, CORRELATION_ID)

    # Assert
    assert loaded == []


def test_load_messages_returns_an_empty_list_for_an_unknown_correlation_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bus = _three_message_bus(clock)
    with session_factory() as session:
        persist_messages(session, list(bus.history), MERCHANT, None)
        session.commit()

    # Act
    with session_factory() as session:
        loaded = load_messages(session, SCOPE, OTHER_CORRELATION_ID)

    # Assert
    assert loaded == []


def test_load_messages_does_not_return_a_user_scoped_trail_to_a_merchant_scope(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    bus = _three_message_bus(clock)
    with session_factory() as session:
        persist_messages(session, list(bus.history), None, USER)
        session.commit()

    # Act
    with session_factory() as session:
        loaded = load_messages(session, SCOPE, CORRELATION_ID)

    # Assert
    assert loaded == []


def test_load_messages_keeps_two_correlation_ids_apart(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first = _three_message_bus(clock, CORRELATION_ID)
    second = InProcessBus(OTHER_CORRELATION_ID, clock)
    second.send("supervisor", "fraud", "investigate", {})
    with session_factory() as session:
        persist_messages(session, list(first.history), MERCHANT, None)
        persist_messages(session, list(second.history), MERCHANT, None)
        session.commit()

    # Act
    with session_factory() as session:
        loaded_first = load_messages(session, SCOPE, CORRELATION_ID)
        loaded_second = load_messages(session, SCOPE, OTHER_CORRELATION_ID)

    # Assert
    assert loaded_first == list(first.history)
    assert loaded_second == list(second.history)
