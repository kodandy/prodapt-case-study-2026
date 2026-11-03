"""Tests for the dead-letter handling of consume_payments, written from docs/specs/sq3.md H-1."""

import json
import logging
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

import ops_ai.ingest.consumer as consumer_module
import pytest
from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from ops_ai.ingest.consumer import PAYMENT_FACTS_GROUP, consume_payments
from redis import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

DEAD_TOPIC = "gw.payments.v1.dead"
CONSUMER = "consumer-dead-letter"
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
PAYLOAD_CANARY = "canary-payload-3b7e91"
PAN_CANARY = "canary-pan-5d2f80"
CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _clock_now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _event(n: int) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=n),
        event_type="payment.declined",
        occurred_at=OCCURRED_AT + timedelta(seconds=n),
        entity_version=1,
        payment_id=uuid.UUID(int=1000 + n),
        merchant_id=uuid.UUID(int=7),
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
        card_ref=None,
    )


def _add_valid(redis: Redis, event: PaymentEventV1) -> str:
    return str(
        redis.xadd(PAYMENTS_TOPIC, {"event_id": str(event.event_id), "payload": event.model_dump_json()})
    )


def _add_raw(redis: Redis, fields: dict[str, str]) -> str:
    return str(redis.xadd(PAYMENTS_TOPIC, {key: value for key, value in fields.items()}))


def _bad_card_ref_payload() -> str:
    payload = json.loads(_event(99).model_dump_json())
    payload["card_ref"] = PAYLOAD_CANARY
    return json.dumps(payload)


class BadEntry(NamedTuple):
    fields: dict[str, str]
    expected_payload: str
    expected_error: str | None
    """None when the exception class is not pinned by the spec (a missing payload field)."""


BAD_ENTRIES: dict[str, BadEntry] = {
    "missing_payload": BadEntry({"event_id": "no-payload-here"}, "", None),
    "not_json": BadEntry({"event_id": "x", "payload": PAYLOAD_CANARY}, PAYLOAD_CANARY, "JSONDecodeError"),
    "empty_payload": BadEntry({"event_id": "x", "payload": ""}, "", "JSONDecodeError"),
    "json_but_not_an_object": BadEntry({"event_id": "x", "payload": "[1, 2]"}, "[1, 2]", "ValidationError"),
    "schema_invalid_card_ref": BadEntry(
        {"event_id": "x", "payload": _bad_card_ref_payload()}, _bad_card_ref_payload(), "ValidationError"
    ),
    "extra_field_forbidden": BadEntry(
        {
            "event_id": "x",
            "payload": json.dumps({**json.loads(_event(98).model_dump_json()), "pan": PAN_CANARY}),
        },
        json.dumps({**json.loads(_event(98).model_dump_json()), "pan": PAN_CANARY}),
        "ValidationError",
    ),
}
BAD_IDS = list(BAD_ENTRIES)


def _dead_entries(redis: Redis) -> list[tuple[str, dict[str, str]]]:
    rows = redis.xrange(DEAD_TOPIC, "-", "+")
    assert isinstance(rows, list)
    entries: list[tuple[str, dict[str, str]]] = []
    for entry_id, fields in rows:
        assert isinstance(fields, dict)
        entries.append((str(entry_id), {str(k): str(v) for k, v in fields.items()}))
    return entries


def _pending(redis: Redis) -> int:
    return int(redis.xpending(PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)["pending"])


def _fact_ids(session_factory: sessionmaker[Session]) -> list[uuid.UUID]:
    with session_factory() as session:
        rows = session.execute(text("SELECT payment_id FROM ops_ai.payment_facts ORDER BY payment_id"))
        return list(rows.scalars())


def _row_counts(session_factory: sessionmaker[Session]) -> tuple[int, int, int]:
    with session_factory() as session:
        row = session.execute(
            text(
                "SELECT (SELECT count(*) FROM ops_ai.payment_facts), "
                "(SELECT count(*) FROM ops_ai.processed_events), "
                "(SELECT count(*) FROM ops_ai.search_documents)"
            )
        ).one()
    return int(row[0]), int(row[1]), int(row[2])


def _consume(session_factory: sessionmaker[Session], redis: Redis, start_id: str = ">") -> int:
    return consume_payments(session_factory, redis, _clock_now, CONSUMER, start_id=start_id)


# --- the dead-letter entry ------------------------------------------------------------------------------


def test_the_dead_letter_topic_constant_is_the_gw_payments_v1_dead_stream() -> None:
    # Arrange / Act
    topic = vars(consumer_module).get("PAYMENTS_DEAD_LETTER_TOPIC")

    # Assert
    assert topic == DEAD_TOPIC


@pytest.mark.parametrize("kind", BAD_IDS)
def test_a_malformed_entry_is_dead_lettered_with_its_id_error_class_and_raw_payload(
    session_factory: sessionmaker[Session], redis_client: Redis, kind: str
) -> None:
    # Arrange
    bad = BAD_ENTRIES[kind]
    entry_id = _add_raw(redis_client, bad.fields)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    (dead,) = _dead_entries(redis_client)
    fields = dead[1]
    assert read == 1
    assert set(fields) == {"source_entry_id", "error", "payload"}
    assert fields["source_entry_id"] == entry_id
    assert fields["payload"] == bad.expected_payload
    if bad.expected_error is None:
        assert CLASS_NAME.fullmatch(fields["error"]) is not None
    else:
        assert fields["error"] == bad.expected_error


@pytest.mark.parametrize("kind", BAD_IDS)
def test_a_malformed_entry_is_acknowledged_so_it_is_not_read_again(
    session_factory: sessionmaker[Session], redis_client: Redis, kind: str
) -> None:
    # Arrange
    _add_raw(redis_client, BAD_ENTRIES[kind].fields)

    # Act
    first = _consume(session_factory, redis_client)
    pending_after_first = _pending(redis_client)
    reread = _consume(session_factory, redis_client, start_id="0")

    # Assert
    assert first == 1
    assert pending_after_first == 0
    assert reread == 0
    assert len(_dead_entries(redis_client)) == 1


@pytest.mark.parametrize("kind", BAD_IDS)
def test_a_malformed_entry_writes_nothing_to_postgres(
    session_factory: sessionmaker[Session], redis_client: Redis, kind: str
) -> None:
    # Arrange
    _add_raw(redis_client, BAD_ENTRIES[kind].fields)

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert _row_counts(session_factory) == (0, 0, 0)


def test_the_dead_letter_xadd_is_capped_at_10000_entries_approximately(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _add_raw(redis_client, BAD_ENTRIES["not_json"].fields)
    calls: list[tuple[str, dict[str, object]]] = []
    original: Callable[..., object] = redis_client.xadd

    def _spy(name: str, fields: dict[str, str], *args: object, **kwargs: object) -> object:
        calls.append((name, dict(kwargs)))
        return original(name, fields, *args, **kwargs)

    monkeypatch.setattr(redis_client, "xadd", _spy)

    # Act
    _consume(session_factory, redis_client)

    # Assert
    assert len(calls) == 1
    name, kwargs = calls[0]
    assert name == DEAD_TOPIC
    assert kwargs.get("maxlen") == 10_000
    assert kwargs.get("approximate", True) is True


# --- the rest of the batch ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", BAD_IDS)
def test_valid_entries_around_a_malformed_one_are_still_projected_and_the_dead_one_counts_in_the_return(
    session_factory: sessionmaker[Session], redis_client: Redis, kind: str
) -> None:
    # Arrange
    first, last = _event(1), _event(2)
    _add_valid(redis_client, first)
    bad_id = _add_raw(redis_client, BAD_ENTRIES[kind].fields)
    _add_valid(redis_client, last)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 3
    assert _fact_ids(session_factory) == sorted([first.payment_id, last.payment_id])
    assert [fields["source_entry_id"] for _, fields in _dead_entries(redis_client)] == [bad_id]
    assert _pending(redis_client) == 0


def test_two_malformed_entries_in_one_batch_are_both_dead_lettered_in_stream_order(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _event(3)
    first_bad = _add_raw(redis_client, BAD_ENTRIES["not_json"].fields)
    _add_valid(redis_client, event)
    second_bad = _add_raw(redis_client, BAD_ENTRIES["missing_payload"].fields)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 3
    assert [fields["source_entry_id"] for _, fields in _dead_entries(redis_client)] == [first_bad, second_bad]
    assert _fact_ids(session_factory) == [event.payment_id]
    assert _pending(redis_client) == 0


def test_a_malformed_entry_left_pending_by_an_earlier_read_is_dead_lettered_on_the_reread(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange: the entry was delivered to this consumer but never acknowledged
    bad_id = _add_raw(redis_client, BAD_ENTRIES["schema_invalid_card_ref"].fields)
    event = _event(4)
    _add_valid(redis_client, event)
    consumer_module.ensure_group(redis_client, PAYMENTS_TOPIC, PAYMENT_FACTS_GROUP)
    redis_client.xreadgroup(PAYMENT_FACTS_GROUP, CONSUMER, {PAYMENTS_TOPIC: ">"}, count=1)
    pending_before = _pending(redis_client)

    # Act
    reread = _consume(session_factory, redis_client, start_id="0")
    fresh = _consume(session_factory, redis_client)

    # Assert
    assert pending_before == 1
    assert reread == 1
    assert fresh == 1
    assert [fields["source_entry_id"] for _, fields in _dead_entries(redis_client)] == [bad_id]
    assert _fact_ids(session_factory) == [event.payment_id]
    assert _pending(redis_client) == 0


# --- logging --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", BAD_IDS)
def test_a_dead_lettered_entry_logs_one_warning_with_the_entry_id_and_class_name_only(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    # Arrange
    caplog.set_level(logging.DEBUG)
    entry_id = _add_raw(redis_client, BAD_ENTRIES[kind].fields)

    # Act
    _consume(session_factory, redis_client)

    # Assert
    (dead,) = _dead_entries(redis_client)
    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == "ops_ai.ingest.consumer"
    ]
    assert [record.getMessage() for record in warnings] == [
        f"dead-lettered event entry_id={entry_id} error={dead[1]['error']}"
    ]
    captured = capsys.readouterr()
    logged = caplog.text + captured.out + captured.err
    assert PAYLOAD_CANARY not in logged
    assert PAN_CANARY not in logged
    assert "validation error" not in logged.lower()
    assert "Traceback" not in logged


# --- errors while applying a valid event still propagate ------------------------------------------------


def test_a_database_error_while_applying_a_valid_event_propagates_and_leaves_the_entry_pending(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _add_valid(redis_client, _event(5))
    error = OperationalError("SELECT 1", {}, Exception("db down"))

    def _begin_fails() -> object:
        raise error

    monkeypatch.setattr(session_factory, "begin", _begin_fails)

    # Act
    with pytest.raises(OperationalError):
        _consume(session_factory, redis_client)

    # Assert
    assert _pending(redis_client) == 1
    assert _dead_entries(redis_client) == []


def test_a_redis_error_while_acknowledging_a_valid_event_propagates_and_dead_letters_nothing(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _add_valid(redis_client, _event(6))

    def _xack_fails(*_args: object, **_kwargs: object) -> object:
        raise RedisConnectionError("redis down")

    monkeypatch.setattr(redis_client, "xack", _xack_fails)

    # Act
    with pytest.raises(RedisConnectionError):
        _consume(session_factory, redis_client)

    # Assert
    assert _pending(redis_client) == 1
    assert _dead_entries(redis_client) == []


def test_an_error_applying_a_valid_event_after_a_malformed_one_still_dead_letters_the_malformed_one(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    bad_id = _add_raw(redis_client, BAD_ENTRIES["not_json"].fields)
    _add_valid(redis_client, _event(7))

    def _apply_fails(*_args: object, **_kwargs: object) -> bool:
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    monkeypatch.setattr(consumer_module, "apply_payment_event", _apply_fails)

    # Act
    with pytest.raises(OperationalError):
        _consume(session_factory, redis_client)

    # Assert
    assert [fields["source_entry_id"] for _, fields in _dead_entries(redis_client)] == [bad_id]
    assert _pending(redis_client) == 1
