"""Tests for the ops-ai compliance consumer and GET /v1/ops/compliance/erasures, written from
docs/specs/compliance-rbac.md sections 5 (ticket per review), 6 (ops-ai erasure handling) and Events."""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
import pytest
from contracts.compliance import COMPLIANCE_TOPIC, KycReviewRequiredV1, SubjectErasedV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.compliance.consumer import (
    COMPLIANCE_DEAD_LETTER_TOPIC,
    COMPLIANCE_GROUP,
    apply_compliance_event,
    consume_compliance,
)
from redis import Redis
from redis.typing import EncodableT, FieldT
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

OCCURRED_AT = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)
CONSUMER = "consumer-compliance"
USER = uuid.UUID("5d2c8e41-7a39-4b60-8f12-3c4d5e6f7a88")
OTHER_USER = uuid.UUID("7e3d9f52-8b4a-4c71-9a23-4d5e6f7a8b99")
THIRD_USER = uuid.UUID("9f4eaa63-9c5b-4d82-8b34-5e6f7a8b9caa")
REVIEW = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
REQUEST = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
OPS_USER = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c91")
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c03")
ERASURES = "/v1/ops/compliance/erasures"
PAYLOAD_CANARY = "canary-payload-3b7e91"
EMAIL_CANARY = "canary-alice@example.com"
ISSUED_AT = int(TEST_NOW.timestamp())
ERASURE_COLUMNS = {"request_id", "processed_at", "rows_pseudonymised"}


def _clock() -> datetime:
    return TEST_NOW


def _kyc_event(n: int = 1, user: uuid.UUID = USER, review: uuid.UUID = REVIEW) -> KycReviewRequiredV1:
    return KycReviewRequiredV1(
        event_id=uuid.UUID(int=n),
        event_type="compliance.kyc_review_required",
        occurred_at=OCCURRED_AT,
        origin="live",
        review_id=review,
        user_id=user,
        rules=["amount_threshold"],
    )


def _erased_event(n: int = 100, user: uuid.UUID = USER, request: uuid.UUID = REQUEST) -> SubjectErasedV1:
    return SubjectErasedV1(
        event_id=uuid.UUID(int=n),
        event_type="compliance.subject_erased",
        occurred_at=OCCURRED_AT,
        origin="live",
        request_id=request,
        user_id=user,
    )


def _apply(session_factory: sessionmaker[Session], event: KycReviewRequiredV1 | SubjectErasedV1) -> bool:
    with session_factory.begin() as session:
        return apply_compliance_event(session, _clock, event)


def _insert_fact(session_factory: sessionmaker[Session], transfer_id: uuid.UUID, sender: uuid.UUID) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, "
                "method, status, country_sender, country_receiver, decided_at, ingest_source) "
                "VALUES (:transfer_id, :sender, 180000, 'USD', 'bank_transfer', 'failed', 'US', 'IN', "
                ":decided_at, 'seed')"
            ),
            {"transfer_id": transfer_id, "sender": sender, "decided_at": OCCURRED_AT},
        )


def _insert_ticket(session_factory: sessionmaker[Session], source_key: str, title: str, reason: str) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.tickets (ticket_id, source_key, source, queue, priority, title, reason, "
                "created_at) VALUES (:ticket_id, :source_key, 'kyc_review', 'compliance', 'normal', :title, "
                ":reason, :created_at)"
            ),
            {
                "ticket_id": uuid.uuid4(),
                "source_key": source_key,
                "title": title,
                "reason": reason,
                "created_at": OCCURRED_AT,
            },
        )


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _tickets(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT source_key, source, queue, status, merchant_id, title, reason FROM ops_ai.tickets "
        "ORDER BY source_key",
    )


def _facts(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT transfer_id, sender_user_id, amount_minor, currency FROM ops_ai.transfer_facts "
        "ORDER BY transfer_id",
    )


def _erasures(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT request_id, processed_at, rows_pseudonymised FROM ops_ai.subject_erasures "
        "ORDER BY request_id",
    )


def _processed(session_factory: sessionmaker[Session]) -> list[tuple[object, object]]:
    rows = _rows(session_factory, "SELECT consumer, event_id FROM ops_ai.processed_events ORDER BY event_id")
    return [(row["consumer"], row["event_id"]) for row in rows]


def _pseudonym_of(fact: dict[str, object]) -> uuid.UUID:
    sender = fact["sender_user_id"]
    assert isinstance(sender, uuid.UUID)
    return sender


# --- constants -------------------------------------------------------------------------------------------


def test_the_group_and_dead_letter_topic_follow_the_specified_names() -> None:
    # Arrange / Act
    names = (COMPLIANCE_GROUP, COMPLIANCE_DEAD_LETTER_TOPIC, COMPLIANCE_TOPIC)

    # Assert
    assert names == ("ops-ai.compliance", "gw.compliance.v1.dead", "gw.compliance.v1")


# --- apply_compliance_event: kyc_review_required ---------------------------------------------------------


def test_a_kyc_review_event_opens_one_compliance_ticket_with_the_review_source_key(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _kyc_event()

    # Act
    applied = _apply(session_factory, event)

    # Assert
    [ticket] = _tickets(session_factory)
    assert applied is True
    assert (ticket["queue"], ticket["source"], ticket["source_key"]) == (
        "compliance",
        "kyc_review",
        f"kyc_review:{REVIEW}",
    )
    assert (ticket["status"], ticket["merchant_id"]) == ("open", None)


def test_a_duplicate_kyc_review_event_returns_false_and_leaves_one_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _kyc_event()
    _apply(session_factory, event)

    # Act
    second = _apply(session_factory, event)

    # Assert
    assert second is False
    assert [ticket["source_key"] for ticket in _tickets(session_factory)] == [f"kyc_review:{REVIEW}"]
    assert _processed(session_factory) == [(COMPLIANCE_GROUP, event.event_id)]


def test_a_second_event_for_the_same_review_does_not_open_a_second_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _apply(session_factory, _kyc_event(1))

    # Act
    applied = _apply(session_factory, _kyc_event(2))

    # Assert
    assert applied is True
    assert [ticket["source_key"] for ticket in _tickets(session_factory)] == [f"kyc_review:{REVIEW}"]


def test_events_for_two_reviews_open_two_tickets(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    other_review = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c04")

    # Act
    _apply(session_factory, _kyc_event(1))
    _apply(session_factory, _kyc_event(2, OTHER_USER, other_review))

    # Assert
    assert [ticket["source_key"] for ticket in _tickets(session_factory)] == sorted(
        [f"kyc_review:{REVIEW}", f"kyc_review:{other_review}"]
    )


# --- apply_compliance_event: subject_erased --------------------------------------------------------------


def _seed_facts(session_factory: sessionmaker[Session]) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    first, second, other = (uuid.UUID(int=n) for n in (9001, 9002, 9003))
    _insert_fact(session_factory, first, USER)
    _insert_fact(session_factory, second, USER)
    _insert_fact(session_factory, other, OTHER_USER)
    return first, second, other


def test_an_erasure_event_gives_every_fact_of_the_user_one_identical_new_pseudonym(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first, second, _ = _seed_facts(session_factory)

    # Act
    applied = _apply(session_factory, _erased_event())

    # Assert
    by_transfer = {fact["transfer_id"]: _pseudonym_of(fact) for fact in _facts(session_factory)}
    assert applied is True
    assert by_transfer[first] == by_transfer[second]
    assert by_transfer[first] not in {USER, OTHER_USER}


def test_an_erasure_event_leaves_other_users_facts_untouched_and_keeps_every_other_column(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first, second, other = _seed_facts(session_factory)

    # Act
    _apply(session_factory, _erased_event())

    # Assert
    facts = {fact["transfer_id"]: fact for fact in _facts(session_factory)}
    assert set(facts) == {first, second, other}
    assert facts[other]["sender_user_id"] == OTHER_USER
    assert [(facts[key]["amount_minor"], facts[key]["currency"]) for key in (first, second, other)] == [
        (180000, "USD"),
        (180000, "USD"),
        (180000, "USD"),
    ]


def test_an_erasure_event_records_the_request_with_the_count_of_pseudonymised_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_facts(session_factory)

    # Act
    _apply(session_factory, _erased_event())

    # Assert
    [erasure] = _erasures(session_factory)
    assert (erasure["request_id"], erasure["rows_pseudonymised"], erasure["processed_at"]) == (
        REQUEST,
        2,
        TEST_NOW,
    )


def test_the_erasure_record_keeps_no_user_id_column(session_factory: sessionmaker[Session]) -> None:
    # Arrange / Act
    columns = _rows(
        session_factory,
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'ops_ai' AND table_name = 'subject_erasures'",
    )

    # Assert
    assert {row["column_name"] for row in columns} == ERASURE_COLUMNS


def test_an_erasure_event_for_a_user_without_data_records_zero_pseudonymised_rows(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    event = _erased_event(user=THIRD_USER)

    # Act
    applied = _apply(session_factory, event)

    # Assert
    [erasure] = _erasures(session_factory)
    assert applied is True
    assert (erasure["request_id"], erasure["rows_pseudonymised"]) == (REQUEST, 0)


def test_a_duplicate_erasure_event_returns_false_and_changes_nothing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _seed_facts(session_factory)
    event = _erased_event()
    _apply(session_factory, event)
    facts_before = _facts(session_factory)

    # Act
    second = _apply(session_factory, event)

    # Assert
    assert second is False
    assert _facts(session_factory) == facts_before
    assert [erasure["request_id"] for erasure in _erasures(session_factory)] == [REQUEST]


def test_two_erased_users_get_different_pseudonyms(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _seed_facts(session_factory)
    other_request = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c05")

    # Act
    _apply(session_factory, _erased_event(100))
    _apply(session_factory, _erased_event(101, OTHER_USER, other_request))

    # Assert
    pseudonyms = {_pseudonym_of(fact) for fact in _facts(session_factory)}
    assert len(pseudonyms) == 2
    assert pseudonyms.isdisjoint({USER, OTHER_USER})


def test_an_erasure_event_removes_the_user_id_from_the_text_of_every_compliance_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_ticket(session_factory, "kyc_review:seeded-1", f"KYC review for {USER}", f"{USER} tripped a rule")
    _insert_ticket(session_factory, "kyc_review:seeded-2", "Name change", f"{USER} renamed; {USER} again")
    _apply(session_factory, _kyc_event(1))

    # Act
    _apply(session_factory, _erased_event())

    # Assert
    texts = [f"{ticket['title']} {ticket['reason']}" for ticket in _tickets(session_factory)]
    assert len(texts) == 3
    assert [value for value in texts if str(USER) in value] == []


def test_an_erasure_event_replaces_the_user_id_in_ticket_text_with_the_facts_pseudonym(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_fact(session_factory, uuid.UUID(int=9001), USER)
    title, reason = f"KYC review for {USER}", f"User {USER} tripped a rule"
    _insert_ticket(session_factory, "kyc_review:seeded-1", title, reason)

    # Act
    _apply(session_factory, _erased_event())

    # Assert
    [fact] = _facts(session_factory)
    pseudonym = str(_pseudonym_of(fact))
    [ticket] = _tickets(session_factory)
    expected = (title.replace(str(USER), pseudonym), reason.replace(str(USER), pseudonym))
    assert (ticket["title"], ticket["reason"]) == expected


def test_an_erasure_event_leaves_other_users_ticket_text_untouched(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    title, reason = f"KYC review for {OTHER_USER}", f"User {OTHER_USER} tripped a rule"
    _insert_ticket(session_factory, "kyc_review:seeded-other", title, reason)
    _insert_ticket(session_factory, "kyc_review:seeded-user", f"KYC review for {USER}", f"User {USER}")

    # Act
    _apply(session_factory, _erased_event())

    # Assert
    tickets = {ticket["source_key"]: ticket for ticket in _tickets(session_factory)}
    assert (tickets["kyc_review:seeded-other"]["title"], tickets["kyc_review:seeded-other"]["reason"]) == (
        title,
        reason,
    )


# --- consume_compliance ----------------------------------------------------------------------------------


def _add(redis: Redis, event: KycReviewRequiredV1 | SubjectErasedV1) -> str:
    fields: dict[FieldT, EncodableT] = {"event_id": str(event.event_id), "payload": event.model_dump_json()}
    return str(redis.xadd(COMPLIANCE_TOPIC, fields))


def _add_raw(redis: Redis, fields: dict[str, str]) -> str:
    encoded: dict[FieldT, EncodableT] = {key: value for key, value in fields.items()}
    return str(redis.xadd(COMPLIANCE_TOPIC, encoded))


def _pending(redis: Redis) -> int:
    return int(redis.xpending(COMPLIANCE_TOPIC, COMPLIANCE_GROUP)["pending"])


def _dead_entries(redis: Redis) -> list[tuple[str, dict[str, str]]]:
    rows = redis.xrange(COMPLIANCE_DEAD_LETTER_TOPIC, "-", "+")
    assert isinstance(rows, list)
    entries: list[tuple[str, dict[str, str]]] = []
    for entry_id, fields in rows:
        assert isinstance(fields, dict)
        entries.append((str(entry_id), {str(k): str(v) for k, v in fields.items()}))
    return entries


def _consume(session_factory: sessionmaker[Session], redis: Redis) -> int:
    return consume_compliance(session_factory, redis, _clock, CONSUMER)


def test_consuming_a_kyc_review_entry_commits_the_ticket_and_acks_the_entry(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    _add(redis_client, _kyc_event())

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 1
    assert [ticket["source_key"] for ticket in _tickets(session_factory)] == [f"kyc_review:{REVIEW}"]
    assert _pending(redis_client) == 0


def test_consuming_an_erasure_entry_pseudonymises_the_facts_and_acks_the_entry(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    _seed_facts(session_factory)
    _add(redis_client, _erased_event())

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    senders = [fact["sender_user_id"] for fact in _facts(session_factory)]
    assert read == 1
    assert OTHER_USER in senders
    assert USER not in senders
    assert [erasure["rows_pseudonymised"] for erasure in _erasures(session_factory)] == [2]
    assert _pending(redis_client) == 0


def test_consuming_the_same_event_twice_reads_both_entries_but_applies_it_once(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    event = _kyc_event()
    _add(redis_client, event)
    _add(redis_client, event)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 2
    assert [ticket["source_key"] for ticket in _tickets(session_factory)] == [f"kyc_review:{REVIEW}"]
    assert _pending(redis_client) == 0


def test_consuming_with_no_entries_returns_zero_and_creates_the_group(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange / Act
    read = _consume(session_factory, redis_client)

    # Assert
    groups = redis_client.xinfo_groups(COMPLIANCE_TOPIC)
    assert read == 0
    assert [group["name"] for group in groups] == [COMPLIANCE_GROUP]


def test_a_malformed_entry_is_copied_to_the_dead_letter_stream_and_acked(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    entry_id = _add_raw(redis_client, {"event_id": "x", "payload": PAYLOAD_CANARY})

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    [(_, dead)] = _dead_entries(redis_client)
    assert read == 1
    assert dead["source_entry_id"] == entry_id
    assert dead["payload"] == PAYLOAD_CANARY
    assert _pending(redis_client) == 0
    assert _tickets(session_factory) == []


def _unknown_type_payload() -> str:
    return json.dumps({**json.loads(_kyc_event().model_dump_json()), "event_type": "compliance.other"})


def _extra_field_payload() -> str:
    return json.dumps({**json.loads(_erased_event().model_dump_json()), "email": EMAIL_CANARY})


@pytest.mark.parametrize(
    "fields",
    [
        {"event_id": "x", "payload": ""},
        {"event_id": "x", "payload": "[1, 2]"},
        {"event_id": "x", "payload": _unknown_type_payload()},
        {"event_id": "x", "payload": _extra_field_payload()},
        {"event_id": "no-payload-here"},
    ],
    ids=["empty_payload", "json_not_an_object", "unknown_event_type", "extra_field", "missing_payload"],
)
def test_each_kind_of_malformed_entry_is_dead_lettered_and_acked_without_side_effects(
    session_factory: sessionmaker[Session], redis_client: Redis, fields: dict[str, str]
) -> None:
    # Arrange
    entry_id = _add_raw(redis_client, fields)

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    dead = _dead_entries(redis_client)
    assert read == 1
    assert [entry["source_entry_id"] for _, entry in dead] == [entry_id]
    assert _pending(redis_client) == 0
    written = (_tickets(session_factory), _erasures(session_factory), _processed(session_factory))
    assert written == ([], [], [])


def test_a_valid_entry_after_a_malformed_one_in_the_same_batch_is_still_applied(
    session_factory: sessionmaker[Session], redis_client: Redis
) -> None:
    # Arrange
    _add_raw(redis_client, {"event_id": "x", "payload": PAYLOAD_CANARY})
    _add(redis_client, _kyc_event())

    # Act
    read = _consume(session_factory, redis_client)

    # Assert
    assert read == 2
    assert len(_dead_entries(redis_client)) == 1
    assert [ticket["source_key"] for ticket in _tickets(session_factory)] == [f"kyc_review:{REVIEW}"]
    assert _pending(redis_client) == 0


# --- GET /v1/ops/compliance/erasures ---------------------------------------------------------------------


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return KeyPair(private_pem=private_pem, public_pem=public_pem)


class _UnusedLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise AssertionError("the erasures route never calls the LLM")


def _client(session_factory: sessionmaker[Session], key_pair: KeyPair) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=_UnusedLlm(),
        jwt_public_key_pem=key_pair.public_pem,
        clock=_clock,
    )
    return TestClient(create_app(deps))


def _sign(claims: dict[str, object], key_pair: KeyPair) -> str:
    return jwt.encode(claims, key_pair.private_pem, algorithm="RS256")


def _base_claims() -> dict[str, object]:
    return {
        "iss": "gateway",
        "aud": "ops-ai",
        "iat": ISSUED_AT,
        "exp": ISSUED_AT + 900,
        "jti": str(uuid.uuid4()),
    }


def _ops_headers(key_pair: KeyPair, role: str = "ops_agent") -> dict[str, str]:
    claims = {**_base_claims(), "sub": f"ops:{OPS_USER}", "ops_user_id": str(OPS_USER), "roles": [role]}
    return {"Authorization": f"Bearer {_sign(claims, key_pair)}"}


def _merchant_headers(key_pair: KeyPair) -> dict[str, str]:
    claims = {
        **_base_claims(),
        "sub": f"merchant:{MERCHANT}",
        "merchant_id": str(MERCHANT),
        "roles": ["merchant_admin"],
    }
    return {"Authorization": f"Bearer {_sign(claims, key_pair)}"}


def _user_headers(key_pair: KeyPair) -> dict[str, str]:
    claims = {**_base_claims(), "sub": f"user:{USER}", "user_id": str(USER), "roles": ["consumer"]}
    return {"Authorization": f"Bearer {_sign(claims, key_pair)}"}


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_an_ops_token_lists_the_recorded_erasures_without_any_user_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str
) -> None:
    # Arrange
    _seed_facts(session_factory)
    _apply(session_factory, _erased_event())
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ERASURES, headers=_ops_headers(key_pair, role))

    # Assert
    [item] = response.json()["items"]
    assert response.status_code == 200
    assert set(response.json()) == {"items"}
    assert set(item) == ERASURE_COLUMNS
    assert (item["request_id"], item["rows_pseudonymised"]) == (str(REQUEST), 2)
    assert datetime.fromisoformat(item["processed_at"]) == TEST_NOW
    assert str(USER) not in response.text


def test_the_erasures_route_lists_every_recorded_erasure(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    other_request = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c05")
    _apply(session_factory, _erased_event(100))
    _apply(session_factory, _erased_event(101, OTHER_USER, other_request))
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ERASURES, headers=_ops_headers(key_pair))

    # Assert
    assert response.status_code == 200
    assert sorted(item["request_id"] for item in response.json()["items"]) == sorted(
        [str(REQUEST), str(other_request)]
    )


def test_the_erasures_route_is_empty_before_any_erasure(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ERASURES, headers=_ops_headers(key_pair))

    # Assert
    assert (response.status_code, response.json()) == (200, {"items": []})


def test_a_merchant_token_gets_403_forbidden_from_the_erasures_route(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _apply(session_factory, _erased_event())
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ERASURES, headers=_merchant_headers(key_pair))

    # Assert
    assert (response.status_code, response.json()) == (403, {"detail": "forbidden"})


def test_a_user_token_gets_403_forbidden_from_the_erasures_route(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ERASURES, headers=_user_headers(key_pair))

    # Assert
    assert (response.status_code, response.json()) == (403, {"detail": "forbidden"})


def test_a_missing_token_gets_401_from_the_erasures_route(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = client.get(ERASURES)

    # Assert
    assert (response.status_code, response.json()) == (401, {"detail": "invalid token"})
