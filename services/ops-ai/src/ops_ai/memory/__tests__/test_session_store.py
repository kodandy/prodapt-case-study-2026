"""Redis session store, written from docs/specs/memory.md section B10.1 (against a real Redis)."""

import dataclasses
import time
import uuid
from datetime import timedelta

import pytest
from ops_ai.memory.session import (
    MAX_PENDING,
    MAX_TURNS,
    SESSION_TTL_SECONDS,
    ChatSession,
    PendingMemory,
    SessionOwner,
    SessionStore,
    Turn,
    owner_for,
    session_key,
)
from ops_ai.memory.state import EMPTY_STATE, ConversationState, TimeWindow
from ops_ai.scope import Scope
from redis import Redis

from conftest import TEST_NOW

WINDOW = TimeWindow(start=TEST_NOW - timedelta(hours=24), end=TEST_NOW)


def _owner(merchant_id: uuid.UUID | None = None, user_id: str | None = None) -> SessionOwner:
    merchant = merchant_id or uuid.uuid4()
    return SessionOwner(merchant_id=merchant, user_id=user_id or f"merchant:{merchant}")


def _turn(index: int) -> Turn:
    return Turn(
        turn_id=uuid.uuid4(),
        question=f"question {index}",
        intent="metric_query",
        metric="failure_rate",
        window=WINDOW,
        answer=f"answer {index}",
        at=TEST_NOW + timedelta(minutes=index),
    )


def _pending(index: int) -> PendingMemory:
    return PendingMemory(proposal_id=uuid.uuid4(), content=f"memory {index}", source_turn_id=uuid.uuid4())


def _raw(redis_client: Redis, owner: SessionOwner, session_id: uuid.UUID) -> str:
    value = redis_client.get(session_key(owner, session_id))
    assert isinstance(value, str)
    return value


def _ttl(redis_client: Redis, owner: SessionOwner, session_id: uuid.UUID) -> int:
    ttl = redis_client.ttl(session_key(owner, session_id))
    assert isinstance(ttl, int)
    return ttl


# --- owner and key ---------------------------------------------------------------------------------------


def test_owner_for_uses_the_merchant_and_the_token_subject_as_user_slot() -> None:
    # Arrange
    merchant_id = uuid.uuid4()

    # Act
    owner = owner_for(Scope(merchant_id=merchant_id))

    # Assert
    assert owner == SessionOwner(merchant_id=merchant_id, user_id=f"merchant:{merchant_id}")


def test_session_key_has_the_specified_shape() -> None:
    # Arrange
    owner = _owner()
    session_id = uuid.uuid4()

    # Act
    key = session_key(owner, session_id)

    # Assert
    assert key == f"ops_ai:session:{owner.merchant_id}:{owner.user_id}:{session_id}"


def test_session_constants_have_the_specified_values() -> None:
    # Arrange: constants only

    # Act
    values = (SESSION_TTL_SECONDS, MAX_TURNS, MAX_PENDING)

    # Assert
    assert values == (1800, 10, 5)


# --- create and load -------------------------------------------------------------------------------------


def test_create_returns_an_empty_session_for_the_owner(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)

    # Act
    chat = store.create(owner, TEST_NOW)

    # Assert
    assert isinstance(chat.session_id, uuid.UUID)
    assert (chat.owner, chat.created_at) == (owner, TEST_NOW)
    assert (chat.turns, chat.state, chat.pending) == ((), EMPTY_STATE, ())


def test_create_generates_a_different_session_id_each_time(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)

    # Act
    first = store.create(owner, TEST_NOW)
    second = store.create(owner, TEST_NOW)

    # Assert
    assert first.session_id != second.session_id


def test_create_then_load_round_trips_the_session(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    created = store.create(owner, TEST_NOW)

    # Act
    loaded = store.load(owner, created.session_id)

    # Assert
    assert loaded == created


def test_save_then_load_round_trips_turns_state_and_pending(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    created = store.create(owner, TEST_NOW)
    chat = dataclasses.replace(
        created,
        turns=(_turn(0), _turn(1)),
        state=ConversationState(metric="failure_rate", window=WINDOW),
        pending=(_pending(0),),
    )
    store.save(chat)

    # Act
    loaded = store.load(owner, chat.session_id)

    # Assert
    assert loaded == chat


def test_load_of_an_unknown_session_id_returns_none(redis_client: Redis) -> None:
    # Arrange
    store = SessionStore(redis_client)

    # Act
    loaded = store.load(_owner(), uuid.uuid4())

    # Assert
    assert loaded is None


def test_session_is_stored_as_json_under_exactly_its_key(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)

    # Act
    chat = store.create(owner, TEST_NOW)

    # Assert
    assert redis_client.keys("*") == [session_key(owner, chat.session_id)]
    assert _raw(redis_client, owner, chat.session_id).startswith("{")


# --- TTL and expiry --------------------------------------------------------------------------------------


def test_create_sets_a_ttl_of_at_most_ttl_seconds(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)

    # Act
    chat = store.create(owner, TEST_NOW)

    # Assert
    ttl = _ttl(redis_client, owner, chat.session_id)
    assert 0 < ttl <= SESSION_TTL_SECONDS


def test_the_ttl_follows_the_configured_ttl_seconds(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client, ttl_seconds=120)

    # Act
    chat = store.create(owner, TEST_NOW)

    # Assert
    ttl = _ttl(redis_client, owner, chat.session_id)
    assert 0 < ttl <= 120


def test_save_refreshes_the_ttl_back_to_ttl_seconds(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client, ttl_seconds=600)
    chat = store.create(owner, TEST_NOW)
    redis_client.expire(session_key(owner, chat.session_id), 5)
    assert _ttl(redis_client, owner, chat.session_id) <= 5

    # Act
    store.save(dataclasses.replace(chat, turns=(_turn(0),)))

    # Assert
    ttl = _ttl(redis_client, owner, chat.session_id)
    assert 5 < ttl <= 600


def test_load_returns_none_once_the_session_has_expired(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client, ttl_seconds=1)
    chat = store.create(owner, TEST_NOW)
    assert store.load(owner, chat.session_id) == chat

    # Act
    time.sleep(1.2)
    loaded = store.load(owner, chat.session_id)

    # Assert
    assert loaded is None


def test_load_returns_none_after_the_key_is_deleted(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    chat = store.create(owner, TEST_NOW)
    redis_client.delete(session_key(owner, chat.session_id))

    # Act
    loaded = store.load(owner, chat.session_id)

    # Assert
    assert loaded is None


# --- trimming --------------------------------------------------------------------------------------------


def test_save_keeps_only_the_last_max_turns_in_order(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    chat = store.create(owner, TEST_NOW)
    turns = tuple(_turn(index) for index in range(MAX_TURNS + 3))

    # Act
    store.save(dataclasses.replace(chat, turns=turns))

    # Assert
    loaded = store.load(owner, chat.session_id)
    assert loaded is not None
    assert loaded.turns == turns[-MAX_TURNS:]


def test_save_keeps_exactly_max_turns_untouched(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    chat = store.create(owner, TEST_NOW)
    turns = tuple(_turn(index) for index in range(MAX_TURNS))

    # Act
    store.save(dataclasses.replace(chat, turns=turns))

    # Assert
    loaded = store.load(owner, chat.session_id)
    assert loaded is not None
    assert loaded.turns == turns


def test_save_keeps_only_the_last_max_pending_proposals_in_order(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    chat = store.create(owner, TEST_NOW)
    pending = tuple(_pending(index) for index in range(MAX_PENDING + 2))

    # Act
    store.save(dataclasses.replace(chat, pending=pending))

    # Assert
    loaded = store.load(owner, chat.session_id)
    assert loaded is not None
    assert loaded.pending == pending[-MAX_PENDING:]


# --- ownership -------------------------------------------------------------------------------------------


def test_another_merchant_loading_the_session_id_gets_none(redis_client: Redis) -> None:
    # Arrange
    owner_a, owner_b = _owner(), _owner()
    store = SessionStore(redis_client)
    chat_a = store.create(owner_a, TEST_NOW)

    # Act
    loaded = store.load(owner_b, chat_a.session_id)

    # Assert
    assert loaded is None
    assert store.load(owner_a, chat_a.session_id) == chat_a


def test_the_same_merchant_with_another_user_slot_gets_none(redis_client: Redis) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    owner = _owner(merchant_id)
    other_user = _owner(merchant_id, user_id="merchant:someone-else")
    store = SessionStore(redis_client)
    chat = store.create(owner, TEST_NOW)

    # Act
    loaded = store.load(other_user, chat.session_id)

    # Assert
    assert loaded is None


def test_a_value_planted_under_another_merchants_key_is_rejected(redis_client: Redis) -> None:
    # Arrange
    owner_a, owner_b = _owner(), _owner()
    store = SessionStore(redis_client)
    chat_a = store.create(owner_a, TEST_NOW)
    redis_client.set(session_key(owner_b, chat_a.session_id), _raw(redis_client, owner_a, chat_a.session_id))

    # Act
    loaded = store.load(owner_b, chat_a.session_id)

    # Assert
    assert loaded is None


def test_a_value_planted_under_another_session_id_is_rejected(redis_client: Redis) -> None:
    # Arrange
    owner = _owner()
    store = SessionStore(redis_client)
    first = store.create(owner, TEST_NOW)
    other_session_id = uuid.uuid4()
    redis_client.set(session_key(owner, other_session_id), _raw(redis_client, owner, first.session_id))

    # Act
    loaded = store.load(owner, other_session_id)

    # Assert
    assert loaded is None


@pytest.mark.parametrize(
    "value",
    ["{not json", "[]", "null", "{}", ""],
    ids=["garbage", "list", "null", "empty_object", "empty"],
)
def test_malformed_values_load_as_none(redis_client: Redis, value: str) -> None:
    # Arrange
    owner = _owner()
    session_id = uuid.uuid4()
    redis_client.set(session_key(owner, session_id), value)
    store = SessionStore(redis_client)

    # Act
    loaded = store.load(owner, session_id)

    # Assert
    assert loaded is None


def test_save_writes_under_the_key_of_the_session_owner(redis_client: Redis) -> None:
    # Arrange
    owner_a, owner_b = _owner(), _owner()
    store = SessionStore(redis_client)
    chat_a = store.create(owner_a, TEST_NOW)
    updated = ChatSession(
        session_id=chat_a.session_id,
        owner=chat_a.owner,
        created_at=chat_a.created_at,
        turns=(_turn(0),),
        state=chat_a.state,
        pending=(),
    )

    # Act
    store.save(updated)

    # Assert
    assert redis_client.keys("*") == [session_key(owner_a, chat_a.session_id)]
    assert store.load(owner_a, chat_a.session_id) == updated
    assert store.load(owner_b, chat_a.session_id) is None
