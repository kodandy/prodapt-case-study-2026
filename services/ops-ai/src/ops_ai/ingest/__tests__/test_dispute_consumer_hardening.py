"""gw.disputes.v1 consumer hardening (security audit M-2 / M-3 / L-3, red team H1-H4): no entry can stop the
worker or be retried forever, the dead-letter keeps no raw payload, and a dispute never changes merchant."""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import DISPUTES_TOPIC, PAYMENTS_TOPIC, DisputeEventV1
from ops_ai.ingest import worker
from ops_ai.ingest.disputes import (
    DISPUTE_WORKFLOWS_GROUP,
    DISPUTES_DEAD_LETTER_TOPIC,
    apply_dispute_event,
    consume_disputes,
)
from redis import Redis
from redis.typing import KeyT
from sqlalchemy import text
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

WAIT_SECONDS = 15.0
OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000011")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000012")
DEEP_JSON = "[" * 100_000
DEAD_LETTER_FIELDS = {"source_entry_id", "error", "payload_keys", "payload_sha256"}
DISPUTES_LOGGER = "ops_ai.ingest.disputes"


def _now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _payload(
    dispute_id: uuid.UUID, merchant_id: uuid.UUID = MERCHANT, **overrides: object
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event_id": str(uuid.uuid4()),
        "event_type": "dispute.opened",
        "occurred_at": OCCURRED_AT.isoformat(),
        "entity_version": 1,
        "dispute_id": str(dispute_id),
        "payment_id": str(uuid.UUID(int=dispute_id.int ^ 1)),
        "merchant_id": str(merchant_id),
        "from_state": None,
        "to_state": "opened",
        "actor_role": "card_network",
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 1500,
        "refunded_minor": 0,
        "dispute_amount_minor": 1500,
        "chargeback_minor": 0,
        "chargeback_fee_minor": 0,
    }
    payload.update(overrides)
    return payload


def _review(dispute_id: uuid.UUID, merchant_id: uuid.UUID) -> DisputeEventV1:
    return DisputeEventV1.model_validate(
        _payload(
            dispute_id,
            merchant_id,
            event_type="dispute.evidence_submitted",
            occurred_at=(OCCURRED_AT + timedelta(days=1)).isoformat(),
            entity_version=2,
            from_state="opened",
            to_state="under_review",
            actor_role="merchant",
        )
    )


def _publish(redis: Redis, raw: str) -> None:
    redis.xadd(DISPUTES_TOPIC, {"payload": raw})


def _projected(session_factory: sessionmaker[Session]) -> list[tuple[uuid.UUID, uuid.UUID, int]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT dispute_id, merchant_id, entity_version FROM ops_ai.dispute_workflows"
                " ORDER BY dispute_id"
            )
        )
        return [(row.dispute_id, row.merchant_id, row.entity_version) for row in rows]


def _steps(session_factory: sessionmaker[Session], dispute_id: uuid.UUID) -> list[int]:
    with session_factory() as session:
        return list(
            session.execute(
                text(
                    "SELECT entity_version FROM ops_ai.dispute_workflow_steps "
                    "WHERE dispute_id = :dispute_id ORDER BY entity_version"
                ),
                {"dispute_id": dispute_id},
            ).scalars()
        )


def _dead_letters(redis: Redis) -> list[dict[str, str]]:
    entries = redis.xrange(DISPUTES_DEAD_LETTER_TOPIC)
    assert isinstance(entries, list)
    return [{str(k): str(v) for k, v in (fields or {}).items()} for _id, fields in entries]


def _pending(redis: Redis) -> int:
    return int(redis.xpending(DISPUTES_TOPIC, DISPUTE_WORKFLOWS_GROUP)["pending"])


def test_a_payload_that_breaks_the_json_parser_is_dead_lettered_and_the_next_event_still_applies(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    _publish(redis_client, DEEP_JSON)
    _publish(redis_client, json.dumps(_payload(dispute_id)))

    # Act
    read = consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    assert read == 2
    assert [entry["error"] for entry in _dead_letters(redis_client)] == ["RecursionError"]
    assert _projected(session_factory) == [(dispute_id, MERCHANT, 1)]
    assert _pending(redis_client) == 0


def test_the_worker_survives_a_parser_breaking_entry_and_applies_the_event_behind_it(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    _publish(redis_client, DEEP_JSON)
    _publish(redis_client, json.dumps(_payload(dispute_id)))
    stop = threading.Event()
    thread = threading.Thread(
        target=worker.run_consumer,
        args=(session_factory, redis_client, _now, "test-consumer", stop, 50),
        daemon=True,
    )

    # Act
    thread.start()
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline and thread.is_alive() and not _projected(session_factory):
        time.sleep(0.02)
    alive_before_stop = thread.is_alive()
    stop.set()
    thread.join(WAIT_SECONDS)

    # Assert
    assert (alive_before_stop, thread.is_alive()) == (True, False)
    assert _projected(session_factory) == [(dispute_id, MERCHANT, 1)]
    assert _pending(redis_client) == 0


def test_an_entry_that_is_not_utf8_is_dead_lettered_by_the_worker_redis_client(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    raw_client = Redis.from_url(os.environ["REDIS_URL"])
    try:
        raw_client.xadd(DISPUTES_TOPIC, {"payload": b"\xff\xfe{"})
    finally:
        raw_client.close()
    _publish(redis_client, json.dumps(_payload(dispute_id)))
    worker_redis = worker.make_worker_redis(os.environ["REDIS_URL"])

    # Act
    try:
        read = consume_disputes(session_factory, worker_redis, _now, "test-consumer")
    finally:
        worker_redis.close()

    # Assert
    assert read == 2
    assert [entry["error"] for entry in _dead_letters(redis_client)] == ["JSONDecodeError"]
    assert _projected(session_factory) == [(dispute_id, MERCHANT, 1)]
    assert _pending(redis_client) == 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"entity_version": 2**40}, id="entity-version-2-pow-40"),
        pytest.param({"payment_amount_minor": 2**70, "dispute_amount_minor": 2**70}, id="amount-2-pow-70"),
        pytest.param({"currency": "U\x00D"}, id="nul-currency"),
    ],
)
def test_an_event_the_projection_cannot_store_is_dead_lettered_once_and_never_retried(
    session_factory: sessionmaker[Session], redis_client: Redis, overrides: dict[str, object]
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    poisoned = uuid.uuid4()
    _publish(redis_client, json.dumps({**_payload(poisoned), **overrides}))
    _publish(redis_client, json.dumps(_payload(dispute_id)))

    # Act
    first = consume_disputes(session_factory, redis_client, _now, "test-consumer")
    reread = consume_disputes(session_factory, redis_client, _now, "test-consumer", start_id="0")

    # Assert
    assert (first, reread) == (2, 0)
    assert [entry["error"] for entry in _dead_letters(redis_client)] == ["ValidationError"]
    assert _projected(session_factory) == [(dispute_id, MERCHANT, 1)]
    assert _pending(redis_client) == 0


@pytest.mark.parametrize("error_type", [DataError, IntegrityError])
def test_a_data_or_integrity_error_on_apply_is_dead_lettered_and_acked(
    session_factory: sessionmaker[Session],
    redis_client: Redis,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[DataError] | type[IntegrityError],
) -> None:
    # Arrange
    _publish(redis_client, json.dumps(_payload(uuid.uuid4())))

    def _reject(session: Session, clock: object, event: DisputeEventV1) -> bool:
        raise error_type("INSERT INTO ops_ai.dispute_workflows", None, ValueError("rejected by the database"))

    monkeypatch.setattr("ops_ai.ingest.disputes.apply_dispute_event", _reject)

    # Act
    read = consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    assert read == 1
    assert [entry["error"] for entry in _dead_letters(redis_client)] == [error_type.__name__]
    assert _projected(session_factory) == []
    assert _pending(redis_client) == 0


def test_an_operational_error_on_apply_still_propagates_and_leaves_the_entry_pending(
    session_factory: sessionmaker[Session], redis_client: Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    _publish(redis_client, json.dumps(_payload(uuid.uuid4())))

    def _unavailable(session: Session, clock: object, event: DisputeEventV1) -> bool:
        raise OperationalError("SELECT 1", None, ConnectionError("database restarting"))

    monkeypatch.setattr("ops_ai.ingest.disputes.apply_dispute_event", _unavailable)

    # Act
    with pytest.raises(OperationalError):
        consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    assert _dead_letters(redis_client) == []
    assert _pending(redis_client) == 1


def test_the_dead_letter_keeps_the_payload_keys_and_hash_but_never_the_raw_payload(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    bad = _payload(uuid.uuid4()) | {"evidence_text": "Jane Roe, 1 Main St"}
    raw = json.dumps(bad)
    _publish(redis_client, raw)

    # Act
    consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    dead = _dead_letters(redis_client)
    assert len(dead) == 1
    assert set(dead[0]) == DEAD_LETTER_FIELDS
    assert dead[0]["payload_keys"] == ",".join(sorted(bad))
    assert dead[0]["payload_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert [value for value in dead[0].values() if "Jane Roe" in value] == []


def test_a_payload_that_is_not_an_object_is_dead_lettered_with_no_keys(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    raw = json.dumps(["Jane Roe"])
    _publish(redis_client, raw)

    # Act
    consume_disputes(session_factory, redis_client, _now, "test-consumer")

    # Assert
    dead = _dead_letters(redis_client)
    assert [(entry["error"], entry["payload_keys"], entry["payload_sha256"]) for entry in dead] == [
        ("ValidationError", "", hashlib.sha256(raw.encode()).hexdigest())
    ]


def test_a_higher_version_from_another_merchant_never_moves_the_dispute_and_is_logged_by_id_only(
    session_factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    opened = DisputeEventV1.model_validate(_payload(dispute_id, MERCHANT))
    hijack = _review(dispute_id, OTHER_MERCHANT)
    with session_factory.begin() as session:
        apply_dispute_event(session, _now, opened)
    caplog.set_level(logging.WARNING, logger=DISPUTES_LOGGER)

    # Act
    with session_factory.begin() as session:
        applied = apply_dispute_event(session, _now, hijack)

    # Assert
    assert applied is True
    assert _projected(session_factory) == [(dispute_id, MERCHANT, 1)]
    assert _steps(session_factory, dispute_id) == [1]
    warnings = [
        r.getMessage() for r in caplog.records if r.name == DISPUTES_LOGGER and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert str(dispute_id) in warnings[0]
    assert str(hijack.event_id) in warnings[0]
    assert str(MERCHANT) not in warnings[0]
    assert str(OTHER_MERCHANT) not in warnings[0]


def test_the_same_merchant_still_advances_after_a_rejected_merchant_switch(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    with session_factory.begin() as session:
        apply_dispute_event(session, _now, DisputeEventV1.model_validate(_payload(dispute_id, MERCHANT)))
    with session_factory.begin() as session:
        apply_dispute_event(session, _now, _review(dispute_id, OTHER_MERCHANT))

    # Act
    with session_factory.begin() as session:
        applied = apply_dispute_event(session, _now, _review(dispute_id, MERCHANT))

    # Assert
    assert applied is True
    assert _projected(session_factory) == [(dispute_id, MERCHANT, 2)]
    assert _steps(session_factory, dispute_id) == [1, 2]


class _GroupInfoRecorder(Redis):
    def __init__(self, stop: threading.Event) -> None:
        super().__init__()
        self.stop = stop
        self.streams: list[str] = []

    def xinfo_groups(self, name: KeyT) -> list[dict[str, object]]:
        self.streams.append(str(name))
        self.stop.set()
        return []


def test_the_lag_sampler_covers_the_dispute_workflow_group_too() -> None:
    # Arrange
    stop = threading.Event()
    recorder = _GroupInfoRecorder(stop)

    # Act
    worker.sample_consumer_lag(recorder, stop, interval_seconds=0.0)

    # Assert
    assert recorder.streams == [PAYMENTS_TOPIC, DISPUTES_TOPIC]
