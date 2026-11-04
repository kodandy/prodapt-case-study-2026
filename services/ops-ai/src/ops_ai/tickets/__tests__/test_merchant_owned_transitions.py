"""Merchants may change only assistant_escalation tickets; card_testing, dispute and unknown_payment are
read-only to them. Covers transition_ticket and POST /v1/tickets/{id}/transition."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.scope import Scope
from ops_ai.tickets.routing import TicketSource
from ops_ai.tickets.service import (
    MERCHANT_OWNED_SOURCES,
    InvalidTicketTransitionError,
    NewTicket,
    TicketNotFoundError,
    TicketNotMerchantOwnedError,
    TicketStatus,
    TicketView,
    get_ticket,
    list_tickets,
    open_ticket,
    transition_ticket,
)
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
ASSIGNED_AT = TEST_NOW + timedelta(hours=1)
RESOLVED_AT = TEST_NOW + timedelta(hours=2)
FORCED_AT = TEST_NOW + timedelta(minutes=30)
Target = Literal["assigned", "resolved"]

READ_ONLY_SOURCES: list[TicketSource] = ["card_testing", "dispute", "unknown_payment"]
ALL_SOURCES: list[TicketSource] = ["card_testing", "dispute", "unknown_payment", "assistant_escalation"]
STATUSES: list[TicketStatus] = ["open", "assigned", "resolved"]
TARGETS: list[Target] = ["assigned", "resolved"]

NOT_MERCHANT_OWNED_BODY = {"detail": "ticket_not_merchant_owned"}
NOT_FOUND_BODY = {"detail": "ticket not found"}
INVALID_TRANSITION_BODY = {"detail": "invalid ticket transition"}


def _clock_at(moment: datetime) -> Callable[[], datetime]:
    return lambda: moment


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
        return open_ticket(session, _clock_at(at), new).ticket_id


def _force_status(session_factory: sessionmaker[Session], ticket_id: uuid.UUID, status: TicketStatus) -> None:
    """Puts the ticket in `status` with a plain UPDATE, because the service refuses read-only tickets."""
    if status == "open":
        return
    with session_factory.begin() as session:
        session.execute(
            text(
                "UPDATE ops_ai.tickets SET status = :status, assignee = 'alice', updated_at = :at "
                "WHERE ticket_id = :ticket_id"
            ),
            {"status": status, "at": FORCED_AT, "ticket_id": ticket_id},
        )


def _open_in_status(
    session_factory: sessionmaker[Session],
    key: str,
    source: TicketSource,
    status: TicketStatus,
    *,
    merchant_id: uuid.UUID = MERCHANT,
) -> uuid.UUID:
    ticket_id = _open(session_factory, key, merchant_id=merchant_id, source=source)
    _force_status(session_factory, ticket_id, status)
    return ticket_id


def _transition(
    session_factory: sessionmaker[Session],
    ticket_id: uuid.UUID,
    target: Target,
    assignee: str | None = None,
    *,
    scope: Scope = SCOPE,
    at: datetime = ASSIGNED_AT,
) -> TicketView:
    with session_factory.begin() as session:
        return transition_ticket(session, _clock_at(at), scope, ticket_id, target, assignee)


def _row(session_factory: sessionmaker[Session], ticket_id: uuid.UUID) -> dict[str, object]:
    with session_factory() as session:
        return dict(
            session.execute(
                text("SELECT * FROM ops_ai.tickets WHERE ticket_id = :ticket_id"), {"ticket_id": ticket_id}
            )
            .mappings()
            .one()
        )


def _all_rows(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            dict(row)
            for row in session.execute(text("SELECT * FROM ops_ai.tickets ORDER BY source_key")).mappings()
        ]


# --- service: the constant --------------------------------------------------------------------------------


def test_merchant_owned_sources_is_exactly_assistant_escalation() -> None:
    # Arrange
    expected = frozenset({"assistant_escalation"})

    # Act
    actual = MERCHANT_OWNED_SOURCES

    # Assert
    assert actual == expected
    assert isinstance(actual, frozenset)


# --- service: read-only sources ---------------------------------------------------------------------------


@pytest.mark.parametrize("assignee", [None, "", "bob"], ids=["no_assignee", "empty_assignee", "assignee"])
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_transition_of_a_read_only_ticket_raises_not_merchant_owned_and_changes_nothing(
    session_factory: sessionmaker[Session],
    source: TicketSource,
    status: TicketStatus,
    target: Target,
    assignee: str | None,
) -> None:
    # Arrange
    ticket_id = _open_in_status(session_factory, "k", source, status)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(TicketNotMerchantOwnedError):
        _transition(session_factory, ticket_id, target, assignee, at=RESOLVED_AT)
    assert _row(session_factory, ticket_id) == before


@pytest.mark.parametrize("assignee", [None, "bob"], ids=["no_assignee", "assignee"])
@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("source", ALL_SOURCES)
def test_transition_of_another_merchants_ticket_raises_not_found_whatever_its_source(
    session_factory: sessionmaker[Session],
    source: TicketSource,
    target: Target,
    assignee: str | None,
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", merchant_id=OTHER_MERCHANT, source=source)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(TicketNotFoundError):
        _transition(session_factory, ticket_id, target, assignee, scope=SCOPE)
    assert _row(session_factory, ticket_id) == before


@pytest.mark.parametrize("status", ["assigned", "resolved"])
@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_transition_of_another_merchants_advanced_read_only_ticket_raises_not_found(
    session_factory: sessionmaker[Session], source: TicketSource, status: TicketStatus
) -> None:
    # Arrange
    ticket_id = _open_in_status(session_factory, "k", source, status, merchant_id=OTHER_MERCHANT)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(TicketNotFoundError):
        _transition(session_factory, ticket_id, "resolved", scope=SCOPE, at=RESOLVED_AT)
    assert _row(session_factory, ticket_id) == before


@pytest.mark.parametrize("target", TARGETS)
def test_transition_of_a_missing_ticket_raises_not_found_even_without_an_assignee(
    session_factory: sessionmaker[Session], target: Target
) -> None:
    # Arrange
    _open(session_factory, "k", source="dispute")

    # Act / Assert
    with pytest.raises(TicketNotFoundError):
        _transition(session_factory, uuid.uuid4(), target, None)


@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_a_rejected_read_only_transition_leaves_the_merchants_other_tickets_untouched(
    session_factory: sessionmaker[Session], source: TicketSource
) -> None:
    # Arrange
    read_only = _open(session_factory, "read-only", source=source)
    owned = _open(session_factory, "owned", source="assistant_escalation")
    rows_before = _all_rows(session_factory)

    # Act / Assert
    with pytest.raises(TicketNotMerchantOwnedError):
        _transition(session_factory, read_only, "assigned", "alice")
    assert _all_rows(session_factory) == rows_before
    assert _row(session_factory, owned)["status"] == "open"


@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_read_only_tickets_are_still_listed_and_readable_by_their_merchant(
    session_factory: sessionmaker[Session], source: TicketSource
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", source=source)

    # Act
    with session_factory() as session:
        view = get_ticket(session, SCOPE, ticket_id)
        listed = list_tickets(session, SCOPE)

    # Assert
    assert view is not None
    assert (view.ticket_id, view.source, view.status) == (ticket_id, source, "open")
    assert [(ticket.ticket_id, ticket.source) for ticket in listed] == [(ticket_id, source)]


# --- service: assistant_escalation keeps the existing rules -----------------------------------------------


def test_an_assistant_escalation_ticket_goes_open_to_assigned_to_resolved(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", source="assistant_escalation")

    # Act
    assigned = _transition(session_factory, ticket_id, "assigned", "alice", at=ASSIGNED_AT)
    resolved = _transition(session_factory, ticket_id, "resolved", at=RESOLVED_AT)

    # Assert
    assert (assigned.status, assigned.assignee, assigned.updated_at) == ("assigned", "alice", ASSIGNED_AT)
    assert (resolved.status, resolved.assignee, resolved.updated_at) == ("resolved", "alice", RESOLVED_AT)
    assert resolved.source == "assistant_escalation"


@pytest.mark.parametrize(
    ("status", "target", "assignee"),
    [
        ("open", "resolved", "alice"),
        ("open", "assigned", None),
        ("open", "assigned", ""),
        ("assigned", "assigned", "bob"),
        ("resolved", "assigned", "bob"),
        ("resolved", "resolved", None),
    ],
    ids=[
        "open_to_resolved",
        "open_to_assigned_no_assignee",
        "open_to_assigned_empty_assignee",
        "assigned_to_assigned",
        "resolved_to_assigned",
        "resolved_to_resolved",
    ],
)
def test_an_invalid_transition_of_an_assistant_escalation_ticket_is_rejected_and_the_row_unchanged(
    session_factory: sessionmaker[Session],
    status: TicketStatus,
    target: Target,
    assignee: str | None,
) -> None:
    # Arrange
    ticket_id = _open_in_status(session_factory, "k", "assistant_escalation", status)
    before = _row(session_factory, ticket_id)

    # Act / Assert
    with pytest.raises(InvalidTicketTransitionError):
        _transition(session_factory, ticket_id, target, assignee, at=RESOLVED_AT)
    assert _row(session_factory, ticket_id) == before


# --- API --------------------------------------------------------------------------------------------------


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


class _IdleLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        return LlmResponse(text="unused", model="idle-model-1")


def _client(session_factory: sessionmaker[Session], public_key_pem: str) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=_IdleLlm(),
        jwt_public_key_pem=public_key_pem,
        clock=lambda: TEST_NOW,
        readiness=lambda: [],
    )
    return TestClient(create_app(deps))


def _merchant_headers(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> dict[str, str]:
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
    return {"Authorization": f"Bearer {jwt.encode(claims, private_pem, algorithm='RS256')}"}


TRANSITION_BODIES: list[dict[str, str]] = [
    {"status": "assigned", "assignee": "alice"},
    {"status": "assigned"},
    {"status": "resolved"},
    {"status": "resolved", "assignee": "alice"},
]
TRANSITION_BODY_IDS = ["assign", "assign_no_assignee", "resolve", "resolve_with_assignee"]


@pytest.mark.parametrize("body", TRANSITION_BODIES, ids=TRANSITION_BODY_IDS)
@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_api_transition_of_a_read_only_ticket_returns_403_no_store_and_changes_nothing(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    source: TicketSource,
    status: TicketStatus,
    body: dict[str, str],
) -> None:
    # Arrange
    ticket_id = _open_in_status(session_factory, "k", source, status)
    before = _row(session_factory, ticket_id)
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.post(
        f"/v1/tickets/{ticket_id}/transition", json=body, headers=_merchant_headers(key_pair.private_pem)
    )

    # Assert
    assert (response.status_code, response.json()) == (403, NOT_MERCHANT_OWNED_BODY)
    assert response.headers["Cache-Control"] == "no-store"
    assert _row(session_factory, ticket_id) == before


@pytest.mark.parametrize("body", TRANSITION_BODIES, ids=TRANSITION_BODY_IDS)
@pytest.mark.parametrize("source", ALL_SOURCES)
def test_api_transition_of_another_merchants_ticket_returns_the_404_identical_to_a_missing_id(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    source: TicketSource,
    body: dict[str, str],
) -> None:
    # Arrange
    foreign = _open(session_factory, "foreign", merchant_id=OTHER_MERCHANT, source=source)
    before = _row(session_factory, foreign)
    client = _client(session_factory, key_pair.public_pem)
    headers = _merchant_headers(key_pair.private_pem)

    # Act
    missing = client.post(f"/v1/tickets/{uuid.uuid4()}/transition", json=body, headers=headers)
    other = client.post(f"/v1/tickets/{foreign}/transition", json=body, headers=headers)

    # Assert
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND_BODY)
    assert (other.status_code, other.json()) == (404, NOT_FOUND_BODY)
    assert other.content == missing.content
    assert other.headers["Cache-Control"] == missing.headers["Cache-Control"]
    assert _row(session_factory, foreign) == before


@pytest.mark.parametrize(
    "body",
    [
        {"status": "assigned", "assignee": "alice", "extra": "x"},
        {"status": "assigned", "assignee": "alice", "merchant_id": str(OTHER_MERCHANT)},
        {"status": "closed"},
        {"status": "assigned", "assignee": "alice smith"},
        {"status": "assigned", "assignee": ""},
        {},
    ],
    ids=[
        "extra_field",
        "merchant_id_field",
        "unknown_status",
        "bad_assignee",
        "empty_assignee",
        "empty_body",
    ],
)
@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_api_transition_validates_the_body_before_the_ownership_check(
    session_factory: sessionmaker[Session], key_pair: KeyPair, source: TicketSource, body: dict[str, str]
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", source=source)
    before = _row(session_factory, ticket_id)
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.post(
        f"/v1/tickets/{ticket_id}/transition", json=body, headers=_merchant_headers(key_pair.private_pem)
    )

    # Assert
    assert response.status_code == 422
    assert _row(session_factory, ticket_id) == before


def test_api_transition_takes_an_assistant_escalation_ticket_from_open_to_assigned_to_resolved(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ticket_id = _open(session_factory, "k", source="assistant_escalation")
    client = _client(session_factory, key_pair.public_pem)
    headers = _merchant_headers(key_pair.private_pem)
    url = f"/v1/tickets/{ticket_id}/transition"

    # Act
    assigned = client.post(url, json={"status": "assigned", "assignee": "alice"}, headers=headers)
    resolved = client.post(url, json={"status": "resolved"}, headers=headers)

    # Assert
    assert assigned.status_code == 200
    assert (assigned.json()["status"], assigned.json()["assignee"]) == ("assigned", "alice")
    assert assigned.json()["source"] == "assistant_escalation"
    assert assigned.headers["Cache-Control"] == "no-store"
    assert resolved.status_code == 200
    assert (resolved.json()["status"], resolved.json()["assignee"]) == ("resolved", "alice")
    row = _row(session_factory, ticket_id)
    assert (row["status"], row["assignee"]) == ("resolved", "alice")


@pytest.mark.parametrize(
    ("status", "body"),
    [
        ("open", {"status": "resolved"}),
        ("open", {"status": "assigned"}),
        ("assigned", {"status": "assigned", "assignee": "bob"}),
        ("resolved", {"status": "resolved"}),
        ("resolved", {"status": "assigned", "assignee": "bob"}),
    ],
    ids=[
        "open_to_resolved",
        "open_to_assigned_no_assignee",
        "assigned_to_assigned",
        "resolved_to_resolved",
        "resolved_to_assigned",
    ],
)
def test_api_invalid_transition_of_an_assistant_escalation_ticket_still_returns_409(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    status: TicketStatus,
    body: dict[str, str],
) -> None:
    # Arrange
    ticket_id = _open_in_status(session_factory, "k", "assistant_escalation", status)
    before = _row(session_factory, ticket_id)
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.post(
        f"/v1/tickets/{ticket_id}/transition", json=body, headers=_merchant_headers(key_pair.private_pem)
    )

    # Assert
    assert (response.status_code, response.json()) == (409, INVALID_TRANSITION_BODY)
    assert response.headers["Cache-Control"] == "no-store"
    assert _row(session_factory, ticket_id) == before


def test_api_rejecting_a_read_only_ticket_does_not_block_the_same_merchants_escalation_ticket(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    read_only = _open(session_factory, "read-only", source="dispute")
    owned = _open(session_factory, "owned", source="assistant_escalation")
    client = _client(session_factory, key_pair.public_pem)
    headers = _merchant_headers(key_pair.private_pem)
    body = {"status": "assigned", "assignee": "alice"}

    # Act
    rejected = client.post(f"/v1/tickets/{read_only}/transition", json=body, headers=headers)
    accepted = client.post(f"/v1/tickets/{owned}/transition", json=body, headers=headers)

    # Assert
    assert (rejected.status_code, rejected.json()) == (403, NOT_MERCHANT_OWNED_BODY)
    assert accepted.status_code == 200
    assert (_row(session_factory, read_only)["status"], _row(session_factory, owned)["status"]) == (
        "open",
        "assigned",
    )


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize("source", READ_ONLY_SOURCES)
def test_api_read_only_tickets_stay_readable_through_get_by_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair, source: TicketSource, status: TicketStatus
) -> None:
    # Arrange
    ticket_id = _open_in_status(session_factory, "k", source, status)
    client = _client(session_factory, key_pair.public_pem)
    headers = _merchant_headers(key_pair.private_pem)
    client.post(f"/v1/tickets/{ticket_id}/transition", json={"status": "resolved"}, headers=headers)

    # Act
    response = client.get(f"/v1/tickets/{ticket_id}", headers=headers)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert (body["ticket_id"], body["source"], body["status"]) == (str(ticket_id), source, status)


def test_api_list_still_returns_read_only_and_escalation_tickets_together(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    ids = {
        source: _open(session_factory, f"k-{source}", source=source, at=TEST_NOW - timedelta(minutes=index))
        for index, source in enumerate(ALL_SOURCES)
    }
    client = _client(session_factory, key_pair.public_pem)

    # Act
    response = client.get("/v1/tickets", headers=_merchant_headers(key_pair.private_pem))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert [(ticket["ticket_id"], ticket["source"]) for ticket in body["tickets"]] == [
        (str(ids[source]), source) for source in ALL_SOURCES
    ]
