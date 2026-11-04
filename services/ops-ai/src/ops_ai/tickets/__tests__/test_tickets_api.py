"""Tests for the /v1/tickets routes and the /ask escalation, from the B12 tickets spec (sections 4a, 6)."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import jwt
import pytest
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.tickets.routing import TicketSource
from ops_ai.tickets.service import NewTicket, open_ticket
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
USER = uuid.UUID("eeeeeeee-0000-4000-8000-000000000005")
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
NOT_FOUND_BODY = {"detail": "ticket not found"}
INVALID_TRANSITION_BODY = {"detail": "invalid ticket transition"}
TICKET_KEYS = {
    "ticket_id",
    "source",
    "queue",
    "priority",
    "status",
    "title",
    "reason",
    "assignee",
    "created_at",
    "updated_at",
}
ASK_KEYS = {"mode", "text", "model", "evidence"}
NARRATION = "narration from the recording llm"


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


def _generate_key_pair() -> KeyPair:
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


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    return _generate_key_pair()


@pytest.fixture(scope="module")
def other_key_pair() -> KeyPair:
    return _generate_key_pair()


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model="recording-model-1")


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


def _client(session_factory: sessionmaker[Session], public_key_pem: str, llm: LlmPort) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=llm,
        jwt_public_key_pem=public_key_pem,
        clock=lambda: TEST_NOW,
        readiness=lambda: [],
    )
    return TestClient(create_app(deps))


def _merchant_token(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _user_token(private_pem: str) -> str:
    issued_at = int(TEST_NOW.timestamp())
    claims: dict[str, object] = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"user:{USER}",
        "user_id": str(USER),
        "roles": ["consumer"],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _open(
    session_factory: sessionmaker[Session],
    key: str,
    *,
    merchant_id: uuid.UUID = MERCHANT,
    source: TicketSource = "dispute",
    at: datetime = TEST_NOW,
) -> uuid.UUID:
    new = NewTicket(
        source=source,
        source_key=key,
        merchant_id=merchant_id,
        title=f"Title {key}",
        reason=f"Reason {key}",
    )
    with session_factory.begin() as session:
        return open_ticket(session, lambda: at, new).ticket_id


def _seed_declined_payment(session_factory: sessionmaker[Session]) -> None:
    event = PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined",
        occurred_at=TEST_NOW - timedelta(minutes=5),
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=MERCHANT,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined",
        provider="acq_a",
        decline_code="card_declined",
        card_bin="400000",
        card_last4="0002",
    )
    with session_factory.begin() as session:
        apply_payment_event(session, lambda: TEST_NOW, event)


def _ticket_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(text("SELECT * FROM ops_ai.tickets ORDER BY source_key")).mappings()
        ]


def _parse(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value)


# --- authentication and authorization ---------------------------------------------------------------------

ROUTES: list[tuple[str, str, dict[str, str] | None]] = [
    ("GET", "/v1/tickets", None),
    ("GET", "/v1/tickets/{ticket_id}", None),
    ("POST", "/v1/tickets/{ticket_id}/transition", {"status": "assigned", "assignee": "alice"}),
]
ROUTE_IDS = ["list", "get", "transition"]


def _call(
    client: TestClient, method: str, path: str, body: dict[str, str] | None, headers: dict[str, str]
) -> tuple[int, dict[str, object]]:
    url = path.replace("{ticket_id}", str(uuid.uuid4()))
    response = client.request(method, url, json=body, headers=headers)
    return response.status_code, response.json()


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_reject_a_missing_token_with_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    status_code, payload = _call(client, method, path, body, {})

    # Assert
    assert (status_code, payload) == (401, INVALID_TOKEN_BODY)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_reject_a_token_signed_with_another_key_with_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    other_key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(other_key_pair.private_pem))

    # Act
    status_code, payload = _call(client, method, path, body, headers)

    # Assert
    assert (status_code, payload) == (401, INVALID_TOKEN_BODY)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_reject_a_garbage_token_with_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    status_code, payload = _call(client, method, path, body, _bearer("not-a-jwt"))

    # Assert
    assert (status_code, payload) == (401, INVALID_TOKEN_BODY)


@pytest.mark.parametrize(("method", "path", "body"), ROUTES, ids=ROUTE_IDS)
def test_routes_reject_a_user_token_with_403(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, str] | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_user_token(key_pair.private_pem))

    # Act
    status_code, payload = _call(client, method, path, body, headers)

    # Assert
    assert (status_code, payload) == (403, FORBIDDEN_BODY)


# --- GET /v1/tickets --------------------------------------------------------------------------------------


def test_list_returns_the_merchants_tickets_newest_first_with_the_exact_ticket_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    older = _open(session_factory, "older", at=TEST_NOW - timedelta(hours=2))
    newer = _open(session_factory, "newer", at=TEST_NOW - timedelta(hours=1))
    _open(session_factory, "foreign", merchant_id=OTHER_MERCHANT)
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.get("/v1/tickets", headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"tickets"}
    assert [ticket["ticket_id"] for ticket in body["tickets"]] == [str(newer), str(older)]
    assert all(set(ticket) == TICKET_KEYS for ticket in body["tickets"])
    assert str(OTHER_MERCHANT) not in response.text
    assert "foreign" not in response.text


def test_list_sets_cache_control_no_store(session_factory: sessionmaker[Session], key_pair: KeyPair) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.get("/v1/tickets", headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 200
    assert response.json() == {"tickets": []}
    assert response.headers["Cache-Control"] == "no-store"


def test_list_filters_by_status_and_queue(session_factory: sessionmaker[Session], key_pair: KeyPair) -> None:
    # Arrange
    dispute = _open(session_factory, "d", source="dispute")
    unknown = _open(session_factory, "u", source="unknown_payment")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))
    assign = {"status": "assigned", "assignee": "alice"}
    client.post(f"/v1/tickets/{unknown}/transition", json=assign, headers=headers)

    # Act
    by_queue = client.get("/v1/tickets", params={"queue": "compliance"}, headers=headers)
    by_status = client.get("/v1/tickets", params={"status": "assigned"}, headers=headers)
    by_both = client.get("/v1/tickets", params={"status": "open", "queue": "payments_ops"}, headers=headers)

    # Assert
    assert [t["ticket_id"] for t in by_queue.json()["tickets"]] == [str(dispute)]
    assert [t["ticket_id"] for t in by_status.json()["tickets"]] == [str(unknown)]
    assert by_both.json() == {"tickets": []}


def test_list_honors_the_limit(session_factory: sessionmaker[Session], key_pair: KeyPair) -> None:
    # Arrange
    ids = [_open(session_factory, f"k{n}", at=TEST_NOW + timedelta(minutes=n)) for n in range(3)]
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.get(
        "/v1/tickets", params={"limit": 2}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert [t["ticket_id"] for t in response.json()["tickets"]] == [str(ids[2]), str(ids[1])]


def test_list_defaults_to_50_tickets_and_accepts_a_limit_of_100(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for n in range(101):
            new = NewTicket(
                source="dispute", source_key=f"bulk-{n}", merchant_id=MERCHANT, title="t", reason="r"
            )
            open_ticket(session, lambda: TEST_NOW, new)
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    default = client.get("/v1/tickets", headers=headers)
    maximum = client.get("/v1/tickets", params={"limit": 100}, headers=headers)
    minimum = client.get("/v1/tickets", params={"limit": 1}, headers=headers)

    # Assert
    assert [len(r.json()["tickets"]) for r in (default, maximum, minimum)] == [50, 100, 1]


@pytest.mark.parametrize(
    "params",
    [
        {"status": "closed"},
        {"status": "OPEN"},
        {"queue": "billing"},
        {"limit": "0"},
        {"limit": "101"},
        {"limit": "-1"},
        {"limit": "many"},
    ],
    ids=["bad_status", "upper_status", "bad_queue", "limit_0", "limit_101", "limit_negative", "limit_nan"],
)
def test_list_rejects_an_invalid_filter_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, params: dict[str, str]
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    response = client.get("/v1/tickets", params=params, headers=headers)

    # Assert
    assert response.status_code == 422


# --- GET /v1/tickets/{id} ---------------------------------------------------------------------------------


def test_get_returns_the_ticket_with_exactly_the_ticket_out_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "dispute:1", source="dispute")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.get(f"/v1/tickets/{ticket_id}", headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert set(body) == TICKET_KEYS
    assert _parse(body.pop("created_at")) == TEST_NOW
    assert body == {
        "ticket_id": str(ticket_id),
        "source": "dispute",
        "queue": "compliance",
        "priority": "high",
        "status": "open",
        "title": "Title dispute:1",
        "reason": "Reason dispute:1",
        "assignee": None,
        "updated_at": None,
    }


def test_get_of_a_missing_and_of_another_merchants_ticket_return_the_identical_404(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    foreign = _open(session_factory, "foreign", merchant_id=OTHER_MERCHANT)
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    missing = client.get(f"/v1/tickets/{uuid.uuid4()}", headers=headers)
    other = client.get(f"/v1/tickets/{foreign}", headers=headers)

    # Assert
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND_BODY)
    assert (other.status_code, other.json()) == (404, NOT_FOUND_BODY)
    assert other.content == missing.content


# --- POST /v1/tickets/{id}/transition ---------------------------------------------------------------------


def test_transition_assigns_then_resolves_a_ticket_and_returns_the_updated_ticket(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    url = f"/v1/tickets/{ticket_id}/transition"
    assigned = client.post(url, json={"status": "assigned", "assignee": "alice"}, headers=headers)
    resolved = client.post(url, json={"status": "resolved"}, headers=headers)

    # Assert
    assert assigned.status_code == 200
    assert assigned.headers["Cache-Control"] == "no-store"
    assert set(assigned.json()) == TICKET_KEYS
    assert (assigned.json()["status"], assigned.json()["assignee"]) == ("assigned", "alice")
    assert _parse(assigned.json()["updated_at"]) == TEST_NOW
    assert resolved.status_code == 200
    assert set(resolved.json()) == TICKET_KEYS
    assert (resolved.json()["status"], resolved.json()["assignee"]) == ("resolved", "alice")
    assert resolved.json()["ticket_id"] == str(ticket_id)
    fetched = client.get(f"/v1/tickets/{ticket_id}", headers=headers)
    assert fetched.json() == resolved.json()


@pytest.mark.parametrize(
    "assignee", ["a", "A.b_c@d-e", "z" * 64], ids=["one_char", "all_allowed_chars", "max_64"]
)
def test_transition_accepts_assignees_at_the_format_bounds(
    session_factory: sessionmaker[Session], key_pair: KeyPair, assignee: str
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())

    # Act
    response = client.post(
        f"/v1/tickets/{ticket_id}/transition",
        json={"status": "assigned", "assignee": assignee},
        headers=_bearer(_merchant_token(key_pair.private_pem)),
    )

    # Assert
    assert response.status_code == 200
    assert response.json()["assignee"] == assignee


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"assignee": "alice"},
        {"status": "open"},
        {"status": "closed"},
        {"status": "ASSIGNED", "assignee": "alice"},
        {"status": "assigned", "assignee": "alice", "extra": "x"},
        {"status": "assigned", "assignee": "alice", "merchant_id": str(OTHER_MERCHANT)},
        {"status": "assigned", "assignee": ""},
        {"status": "assigned", "assignee": "z" * 65},
        {"status": "assigned", "assignee": "alice smith"},
        {"status": "assigned", "assignee": "alice/bob"},
        {"status": "assigned", "assignee": "alice\n"},
        {"status": "assigned", "assignee": 123},
        {"status": 1},
        ["assigned"],
    ],
    ids=[
        "empty",
        "no_status",
        "status_open",
        "status_unknown",
        "status_uppercase",
        "extra_field",
        "merchant_id_field",
        "assignee_empty",
        "assignee_65_chars",
        "assignee_space",
        "assignee_slash",
        "assignee_newline",
        "assignee_not_a_string",
        "status_not_a_string",
        "list_body",
    ],
)
def test_transition_rejects_an_invalid_body_with_422_and_leaves_the_ticket_open(
    session_factory: sessionmaker[Session], key_pair: KeyPair, body: object
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    response = client.post(f"/v1/tickets/{ticket_id}/transition", json=body, headers=headers)

    # Assert
    row = _ticket_rows(session_factory)[0]
    assert response.status_code == 422
    assert (row["status"], row["assignee"], row["updated_at"]) == ("open", None, None)


@pytest.mark.parametrize(
    "body",
    [{"status": "resolved"}, {"status": "resolved", "assignee": "alice"}, {"status": "assigned"}],
    ids=["open_to_resolved", "open_to_resolved_with_assignee", "open_to_assigned_without_assignee"],
)
def test_transition_returns_409_for_an_invalid_transition_and_leaves_the_ticket_open(
    session_factory: sessionmaker[Session], key_pair: KeyPair, body: dict[str, str]
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    response = client.post(f"/v1/tickets/{ticket_id}/transition", json=body, headers=headers)

    # Assert
    row = _ticket_rows(session_factory)[0]
    assert (response.status_code, response.json()) == (409, INVALID_TRANSITION_BODY)
    assert (row["status"], row["assignee"], row["updated_at"]) == ("open", None, None)


def test_transition_returns_409_for_assigning_twice_and_for_touching_a_resolved_ticket(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k")
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))
    url = f"/v1/tickets/{ticket_id}/transition"
    client.post(url, json={"status": "assigned", "assignee": "alice"}, headers=headers)

    # Act
    assign_again = client.post(url, json={"status": "assigned", "assignee": "bob"}, headers=headers)
    resolve = client.post(url, json={"status": "resolved"}, headers=headers)
    resolve_again = client.post(url, json={"status": "resolved"}, headers=headers)
    reassign = client.post(url, json={"status": "assigned", "assignee": "bob"}, headers=headers)

    # Assert
    assert (assign_again.status_code, assign_again.json()) == (409, INVALID_TRANSITION_BODY)
    assert resolve.status_code == 200
    assert (resolve_again.status_code, resolve_again.json()) == (409, INVALID_TRANSITION_BODY)
    assert (reassign.status_code, reassign.json()) == (409, INVALID_TRANSITION_BODY)
    row = _ticket_rows(session_factory)[0]
    assert (row["status"], row["assignee"]) == ("resolved", "alice")


def test_transition_of_a_missing_and_of_another_merchants_ticket_return_the_identical_404(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    foreign = _open(session_factory, "foreign", merchant_id=OTHER_MERCHANT)
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))
    body = {"status": "assigned", "assignee": "mallory"}

    # Act
    missing = client.post(f"/v1/tickets/{uuid.uuid4()}/transition", json=body, headers=headers)
    other = client.post(f"/v1/tickets/{foreign}/transition", json=body, headers=headers)

    # Assert
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND_BODY)
    assert (other.status_code, other.json()) == (404, NOT_FOUND_BODY)
    assert other.content == missing.content
    (row,) = _ticket_rows(session_factory)
    assert (row["status"], row["assignee"], row["updated_at"]) == ("open", None, None)


def test_each_merchant_only_sees_and_changes_its_own_tickets_through_the_api(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    mine = _open(session_factory, "mine", merchant_id=MERCHANT)
    theirs = _open(session_factory, "theirs", merchant_id=OTHER_MERCHANT)
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    my_headers = _bearer(_merchant_token(key_pair.private_pem, MERCHANT))
    their_headers = _bearer(_merchant_token(key_pair.private_pem, OTHER_MERCHANT))

    # Act
    my_list = client.get("/v1/tickets", headers=my_headers)
    their_list = client.get("/v1/tickets", headers=their_headers)
    assigned = client.post(
        f"/v1/tickets/{theirs}/transition",
        json={"status": "assigned", "assignee": "bob"},
        headers=their_headers,
    )

    # Assert
    assert [t["ticket_id"] for t in my_list.json()["tickets"]] == [str(mine)]
    assert [t["ticket_id"] for t in their_list.json()["tickets"]] == [str(theirs)]
    assert assigned.status_code == 200
    assert client.get(f"/v1/tickets/{theirs}", headers=my_headers).status_code == 404
    assert client.get(f"/v1/tickets/{mine}", headers=my_headers).json()["status"] == "open"


# --- POST /v1/assistant/ask opens escalation tickets ------------------------------------------------------


def test_ask_with_an_unavailable_llm_keeps_the_response_keys_and_opens_an_escalation_ticket(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_declined_payment(session_factory)
    client = _client(session_factory, key_pair.public_pem, DownLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))

    # Act
    response = client.post("/v1/assistant/ask", json={"question": "declined payments"}, headers=headers)

    # Assert
    body = response.json()
    (row,) = _ticket_rows(session_factory)
    assert response.status_code == 200
    assert set(body) == ASK_KEYS
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    assert row["source"] == "assistant_escalation"
    assert row["merchant_id"] == MERCHANT
    assert (row["queue"], row["priority"], row["status"]) == ("payments_ops", "normal", "open")
    assert row["title"] == "Assistant escalation"
    assert row["reason"] == "Assistant could not answer (LLM unavailable): declined payments"
    listed = client.get("/v1/tickets", headers=headers).json()["tickets"]
    assert [ticket["ticket_id"] for ticket in listed] == [str(row["ticket_id"])]


def test_ask_with_a_working_llm_and_an_ordinary_question_opens_no_ticket(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_declined_payment(session_factory)
    llm = RecordingLlm()
    client = _client(session_factory, key_pair.public_pem, llm)

    # Act
    response = client.post(
        "/v1/assistant/ask",
        json={"question": "declined payments"},
        headers=_bearer(_merchant_token(key_pair.private_pem)),
    )

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == ASK_KEYS
    assert (body["mode"], body["text"]) == ("llm", NARRATION)
    assert _ticket_rows(session_factory) == []


def test_ask_for_a_human_with_a_working_llm_answers_and_opens_one_ticket_even_when_repeated(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_declined_payment(session_factory)
    client = _client(session_factory, key_pair.public_pem, RecordingLlm())
    headers = _bearer(_merchant_token(key_pair.private_pem))
    question = {"question": "Can I talk to a human?"}

    # Act
    first = client.post("/v1/assistant/ask", json=question, headers=headers)
    second = client.post("/v1/assistant/ask", json=question, headers=headers)

    # Assert
    (row,) = _ticket_rows(session_factory)
    assert (first.status_code, second.status_code) == (200, 200)
    assert set(first.json()) == ASK_KEYS
    assert first.json()["mode"] == "llm"
    assert row["reason"] == "Merchant asked for a human: Can I talk to a human?"
    assert row["merchant_id"] == MERCHANT


def test_an_escalation_ticket_is_invisible_to_another_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_declined_payment(session_factory)
    client = _client(session_factory, key_pair.public_pem, DownLlm())
    client.post(
        "/v1/assistant/ask",
        json={"question": "declined payments"},
        headers=_bearer(_merchant_token(key_pair.private_pem, MERCHANT)),
    )

    # Act
    other = client.get("/v1/tickets", headers=_bearer(_merchant_token(key_pair.private_pem, OTHER_MERCHANT)))

    # Assert
    assert other.json() == {"tickets": []}
