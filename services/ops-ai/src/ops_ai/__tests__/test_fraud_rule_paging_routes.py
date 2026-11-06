"""Paged rule history and audit HTTP contract from r2-agents.md section 7.3."""

import base64
import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.clock import Clock
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

RULES_ROUTE = "/v1/feedback/fraud-rules"
AUDIT_ROUTE = "/v1/feedback/fraud-rules/audit"
ROUTES = [RULES_ROUTE, AUDIT_ROUTE]
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c11")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c12")


@dataclass(frozen=True)
class KeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return KeyPair(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode(),
    )


class IdleLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise AssertionError(f"rule paging must not narrate: {request}")


def _client(session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock) -> TestClient:
    return TestClient(
        create_app(
            OpsAiAppDeps(
                session_factory=session_factory,
                llm=IdleLlm(),
                jwt_public_key_pem=key_pair.public_pem,
                clock=clock,
            )
        )
    )


def _headers(key_pair: KeyPair, merchant_id: uuid.UUID = MERCHANT) -> dict[str, str]:
    issued = int(TEST_NOW.timestamp())
    token = jwt.encode(
        {
            "iss": "gateway",
            "aud": "ops-ai",
            "sub": f"merchant:{merchant_id}",
            "merchant_id": str(merchant_id),
            "roles": ["merchant_admin"],
            "iat": issued,
            "exp": issued + 900,
            "jti": str(uuid.uuid4()),
        },
        key_pair.private_pem,
        algorithm="RS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _insert_versions(session_factory: sessionmaker[Session], merchant_id: uuid.UUID, count: int) -> None:
    with session_factory.begin() as session:
        for version in range(1, count + 1):
            created_at = TEST_NOW + timedelta(minutes=version)
            session.execute(
                text(
                    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
                    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
                    "(:merchant, :version, 1, 12, 0.83, 4, 'revert:v0', NULL, :created_at)"
                ),
                {"merchant": merchant_id, "version": version, "created_at": created_at},
            )
            session.execute(
                text(
                    "INSERT INTO ops_ai.fraud_rule_version_actors "
                    "(merchant_id, version, actor, actor_role, created_at) "
                    "VALUES (:merchant, :version, :actor, 'merchant_admin', :created_at)"
                ),
                {
                    "merchant": merchant_id,
                    "version": version,
                    "actor": f"merchant:{merchant_id}",
                    "created_at": created_at,
                },
            )


def _items_key(route: str) -> str:
    return "history" if route == RULES_ROUTE else "entries"


def _versions(body: dict[str, object], route: str) -> list[int]:
    items = body[_items_key(route)]
    assert isinstance(items, list)
    return [int(item["version"]) for item in items]


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize("route", ROUTES)
def test_walking_pages_with_next_cursor_returns_every_version_once_newest_first(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 5)
    _insert_versions(session_factory, OTHER_MERCHANT, 9)
    headers = _headers(key_pair)

    # Act
    first = client.get(route, headers=headers, params={"limit": 2})
    second = client.get(route, headers=headers, params={"limit": 2, "cursor": first.json()["next_cursor"]})
    third = client.get(route, headers=headers, params={"limit": 2, "cursor": second.json()["next_cursor"]})

    # Assert
    assert [first.status_code, second.status_code, third.status_code] == [200, 200, 200]
    assert _versions(first.json(), route) == [5, 4]
    assert _versions(second.json(), route) == [3, 2]
    assert _versions(third.json(), route) == [1]
    assert isinstance(first.json()["next_cursor"], str)
    assert first.json()["next_cursor"] != second.json()["next_cursor"]
    assert third.json()["next_cursor"] is None
    assert third.headers["cache-control"] == "no-store"


def test_the_paged_rules_keep_the_newest_version_as_active_on_every_page(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 4)
    headers = _headers(key_pair)
    first = client.get(RULES_ROUTE, headers=headers, params={"limit": 3})

    # Act
    second = client.get(RULES_ROUTE, headers=headers, params={"limit": 3, "cursor": first.json()["next_cursor"]})

    # Assert
    assert set(second.json()) == {"active", "history", "next_cursor"}
    assert second.json()["active"]["version"] == 4
    assert second.json()["active"] == first.json()["history"][0]
    assert _versions(second.json(), RULES_ROUTE) == [1]


@pytest.mark.parametrize("route", ROUTES)
def test_without_paging_parameters_the_default_page_is_fifty_and_carries_a_next_cursor(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 51)
    headers = _headers(key_pair)

    # Act
    first = client.get(route, headers=headers)
    rest = client.get(route, headers=headers, params={"cursor": first.json()["next_cursor"]})

    # Assert
    assert _versions(first.json(), route) == list(range(51, 1, -1))
    assert isinstance(first.json()["next_cursor"], str)
    assert _versions(rest.json(), route) == [1]
    assert rest.json()["next_cursor"] is None


@pytest.mark.parametrize("route", ROUTES)
def test_a_single_unpaged_page_keeps_the_legacy_shape_without_next_cursor(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 50)
    headers = _headers(key_pair)

    # Act
    unpaged = client.get(route, headers=headers)
    paged = client.get(route, headers=headers, params={"limit": 50})

    # Assert
    legacy = {"active", "history"} if route == RULES_ROUTE else {"entries"}
    assert set(unpaged.json()) == legacy
    assert set(paged.json()) == legacy | {"next_cursor"}
    assert paged.json()["next_cursor"] is None
    assert _versions(unpaged.json(), route) == list(range(50, 0, -1))


@pytest.mark.parametrize("route", ROUTES)
def test_the_maximum_limit_of_two_hundred_is_accepted(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 201)

    # Act
    response = client.get(route, headers=_headers(key_pair), params={"limit": 200})

    # Assert
    assert response.status_code == 200
    assert _versions(response.json(), route) == list(range(201, 1, -1))
    assert isinstance(response.json()["next_cursor"], str)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("limit", ["0", "-1", "201", "abc", "1.5", ""])
def test_an_out_of_range_or_non_integer_limit_is_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str, limit: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)

    # Act
    response = client.get(route, headers=_headers(key_pair), params={"limit": limit})

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "cursor",
    [
        "garbage",
        "!!!",
        _b64(f"history:{MERCHANT}:0"),
        _b64(f"audit:{MERCHANT}:0"),
        _b64(f"history:{MERCHANT}:9999999999"),
        _b64(f"audit:{MERCHANT}:9999999999"),
        _b64(f"history:{MERCHANT}:3") + "=",
        _b64(f"audit:{MERCHANT}:3") + "=",
    ],
)
def test_a_garbage_or_tampered_cursor_is_422_invalid_cursor(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str, cursor: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 3)

    # Act
    response = client.get(route, headers=_headers(key_pair), params={"cursor": cursor})

    # Assert
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid cursor"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("route", ROUTES)
def test_an_overlong_cursor_is_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)

    # Act
    response = client.get(route, headers=_headers(key_pair), params={"cursor": "a" * 257})

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize("route", ROUTES)
def test_another_merchants_cursor_is_rejected_and_leaks_nothing(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 4)
    _insert_versions(session_factory, OTHER_MERCHANT, 4)
    other_cursor = client.get(route, headers=_headers(key_pair, OTHER_MERCHANT), params={"limit": 1}).json()[
        "next_cursor"
    ]

    # Act
    response = client.get(route, headers=_headers(key_pair), params={"cursor": other_cursor})

    # Assert
    assert isinstance(other_cursor, str)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid cursor"}


def test_a_cursor_from_one_route_is_rejected_by_the_other(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, MERCHANT, 3)
    headers = _headers(key_pair)
    history_cursor = client.get(RULES_ROUTE, headers=headers, params={"limit": 1}).json()["next_cursor"]
    audit_cursor = client.get(AUDIT_ROUTE, headers=headers, params={"limit": 1}).json()["next_cursor"]

    # Act
    on_audit = client.get(AUDIT_ROUTE, headers=headers, params={"cursor": history_cursor})
    on_rules = client.get(RULES_ROUTE, headers=headers, params={"cursor": audit_cursor})

    # Assert
    assert (on_audit.status_code, on_rules.status_code) == (422, 422)
    assert on_audit.json() == {"detail": "invalid cursor"}
    assert on_rules.json() == {"detail": "invalid cursor"}


@pytest.mark.parametrize("route", ROUTES)
def test_a_merchant_id_query_parameter_never_pages_another_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, clock: Clock, route: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_versions(session_factory, OTHER_MERCHANT, 3)

    # Act
    response = client.get(
        route, headers=_headers(key_pair), params={"limit": 1, "merchant_id": str(OTHER_MERCHANT)}
    )

    # Assert
    assert response.status_code == 200
    assert _versions(response.json(), route) == []
    assert response.json()["next_cursor"] is None
