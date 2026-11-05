"""Tests for POST /v1/incidents/search and POST /v1/bulk/analyze, written from docs/specs/r2-agents.md
sections 2, 3 and 5 (R2-4 incident reranking, R2-5 bulk token optimisation)."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.bulk.tokens import count_tokens
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

SEARCH_ROUTE = "/v1/incidents/search"
BULK_ROUTE = "/v1/bulk/analyze"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
EMPTY_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c03")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
TOLERANCE = 1e-9

QUERY = "acquirer timeout"
TITLE = "Acquirer timeout on card payments"
ROOT_CAUSE = "Connection pool exhausted at the acquirer"
FIX_APPLIED = "Raised the pool size"
RESULT_KEYS = {
    "incident_id",
    "title",
    "root_cause",
    "fix_applied",
    "resolved_at",
    "recurrence_within_7d",
    "relevance",
    "recency",
    "effectiveness",
    "score",
}
BULK_KEYS = {
    "transactions",
    "chunks",
    "naive_tokens",
    "optimized_tokens",
    "reduction",
    "within_budget",
    "prompt",
    "mode",
    "text",
    "model",
}
WINDOW_START = TEST_NOW - timedelta(days=2)
WINDOW_END = TEST_NOW
OWN_ROWS = 30
OTHER_ROWS = 5
ROW_TOKENS = 32

INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) "
    "VALUES (:incident_id, :merchant_id, :title, :root_cause, :fix_applied, 'notes', :started_at, "
    ":resolved_at, :recurrence)"
)
INSERT_SCORE = text(
    "INSERT INTO ops_ai.incident_feedback_scores "
    "(merchant_id, incident_id, helpful, not_helpful, updated_at) "
    "VALUES (:merchant_id, :incident_id, :helpful, :not_helpful, :updated_at)"
)
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, 'USD', 'card', :state, 'acq_a', :decline_code, '400000', "
    "NULL, 1, :occurred_at)"
)


@dataclass
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
        return LlmResponse(text=NARRATION, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


def _client(
    session_factory: sessionmaker[Session],
    clock: Callable[[], datetime],
    public_key_pem: str,
    llm: LlmPort,
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory, llm=llm, jwt_public_key_pem=public_key_pem, clock=clock
    )
    return TestClient(create_app(deps))


def _claims(subject: str, extra: dict[str, object], roles: list[str]) -> dict[str, object]:
    issued_at = int(TEST_NOW.timestamp())
    return {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": subject,
        "roles": roles,
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": str(uuid.uuid4()),
        **extra,
    }


def _merchant_token(private_pem: str, merchant_id: uuid.UUID = MERCHANT) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _user_token(private_pem: str) -> str:
    claims = _claims(f"user:{USER}", {"user_id": str(USER)}, ["consumer"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _as_dict(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _as_list(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _app(session_factory: sessionmaker[Session], key_pair: KeyPair, llm: LlmPort | None = None) -> TestClient:
    return _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm or RecordingLlm())


def _bulk_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {"start": WINDOW_START.isoformat(), "end": WINDOW_END.isoformat()}
    body.update(overrides)
    return body


# --- seeding -----------------------------------------------------------------------------------------------


def _seed_incident(
    session_factory: sessionmaker[Session],
    incident_id: str,
    merchant_id: uuid.UUID | None,
    *,
    title: str = TITLE,
    started_at: datetime = TEST_NOW - timedelta(days=3),
    resolved_at: datetime | None = TEST_NOW - timedelta(days=2),
    recurrence: bool = False,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_INCIDENT,
            {
                "incident_id": incident_id,
                "merchant_id": merchant_id,
                "title": title,
                "root_cause": ROOT_CAUSE,
                "fix_applied": FIX_APPLIED,
                "started_at": started_at,
                "resolved_at": resolved_at,
                "recurrence": recurrence,
            },
        )


def _seed_score(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    incident_id: str,
    helpful: int,
    not_helpful: int,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_SCORE,
            {
                "merchant_id": merchant_id,
                "incident_id": incident_id,
                "helpful": helpful,
                "not_helpful": not_helpful,
                "updated_at": TEST_NOW,
            },
        )


def _seed_payments(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, first_n: int, count: int, start: datetime
) -> list[uuid.UUID]:
    """`count` payments one minute apart from `start`, alternately declined and succeeded."""
    ids = [uuid.UUID(int=first_n + index) for index in range(count)]
    with session_factory.begin() as session:
        for index, payment_id in enumerate(ids):
            declined = index % 2 == 0
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": 1000 + index,
                    "state": "declined" if declined else "succeeded",
                    "decline_code": "do_not_honor" if declined else None,
                    "occurred_at": start + timedelta(minutes=index),
                },
            )
    return ids


@dataclass(frozen=True)
class SeededPayments:
    own_ids: list[uuid.UUID]
    others_ids: list[uuid.UUID]
    at_end_id: uuid.UUID


@pytest.fixture
def payments(session_factory: sessionmaker[Session]) -> SeededPayments:
    """30 payments of the merchant in the window, one exactly at its (excluded) end, 5 of another merchant."""
    own_ids = _seed_payments(session_factory, MERCHANT, 1, OWN_ROWS, WINDOW_START)
    (at_end_id,) = _seed_payments(session_factory, MERCHANT, 500, 1, WINDOW_END)
    others_ids = _seed_payments(session_factory, OTHER_MERCHANT, 1000, OTHER_ROWS, WINDOW_START)
    return SeededPayments(own_ids, others_ids, at_end_id)


def _search(
    client: TestClient, key_pair: KeyPair, body: object, merchant_id: uuid.UUID = MERCHANT
) -> dict[str, object]:
    token = _merchant_token(key_pair.private_pem, merchant_id)
    response = client.post(SEARCH_ROUTE, json=body, headers=_bearer(token))
    assert response.status_code == 200
    return _as_dict(response.json())


def _result_ids(body: dict[str, object]) -> list[object]:
    return [_as_dict(item)["incident_id"] for item in _as_list(body["results"])]


def _bulk(
    client: TestClient, key_pair: KeyPair, body: object | None = None, merchant_id: uuid.UUID = MERCHANT
) -> dict[str, object]:
    response = client.post(
        BULK_ROUTE,
        json=body if body is not None else _bulk_body(),
        headers=_bearer(_merchant_token(key_pair.private_pem, merchant_id)),
    )
    assert response.status_code == 200
    return _as_dict(response.json())


# --- POST /v1/incidents/search: shape and values -----------------------------------------------------------


def test_incident_search_returns_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-1", MERCHANT)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY})

    # Assert
    assert set(body) == {"results"}
    (result,) = _as_list(body["results"])
    assert set(_as_dict(result)) == RESULT_KEYS


def test_incident_search_returns_the_incident_with_its_exact_scores(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange: the only hit has relevance 1.0, recency 0.5 (resolved 30 days ago) and effectiveness 0.5
    resolved_at = TEST_NOW - timedelta(days=30)
    _seed_incident(session_factory, "INC-1", MERCHANT, resolved_at=resolved_at)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY})

    # Assert
    (result,) = (_as_dict(item) for item in _as_list(body["results"]))
    assert datetime.fromisoformat(str(result["resolved_at"])) == resolved_at
    assert {k: v for k, v in result.items() if k not in ("resolved_at", "relevance", "recency", "score")} == {
        "incident_id": "INC-1",
        "title": TITLE,
        "root_cause": ROOT_CAUSE,
        "fix_applied": FIX_APPLIED,
        "recurrence_within_7d": False,
        "effectiveness": 0.5,
    }
    assert result["relevance"] == pytest.approx(1.0, abs=TOLERANCE)
    assert result["recency"] == pytest.approx(0.5, abs=TOLERANCE)
    assert result["score"] == pytest.approx(0.75, abs=TOLERANCE)


def test_incident_search_returns_a_null_resolved_at_and_zero_effectiveness_for_an_unresolved_incident(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-1", MERCHANT, resolved_at=None, recurrence=True)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY})

    # Assert
    (result,) = (_as_dict(item) for item in _as_list(body["results"]))
    assert result["resolved_at"] is None
    assert (result["effectiveness"], result["recurrence_within_7d"]) == (0.0, True)


def test_incident_search_with_no_match_returns_an_empty_results_list(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-1", MERCHANT)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": "zzqxv"})

    # Assert
    assert body == {"results": []}


def test_incident_search_orders_by_the_merchants_own_feedback_only(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange: INC-A sorts first by id; only this merchant's votes for INC-B count
    _seed_incident(session_factory, "INC-A", MERCHANT)
    _seed_incident(session_factory, "INC-B", MERCHANT)
    _seed_score(session_factory, MERCHANT, "INC-B", helpful=5, not_helpful=0)
    _seed_score(session_factory, OTHER_MERCHANT, "INC-A", helpful=100, not_helpful=0)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY})

    # Assert
    results = [_as_dict(item) for item in _as_list(body["results"])]
    assert [item["incident_id"] for item in results] == ["INC-B", "INC-A"]
    assert [item["effectiveness"] for item in results] == pytest.approx([6 / 7, 0.5], abs=TOLERANCE)


def test_incident_search_sends_cache_control_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-1", MERCHANT)
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(
        SEARCH_ROUTE, json={"query": QUERY}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


# --- POST /v1/incidents/search: isolation and limit --------------------------------------------------------


def test_incident_search_never_returns_another_merchants_incident_but_returns_platform_wide_ones(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OWN", MERCHANT)
    _seed_incident(session_factory, "INC-PLATFORM", None)
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT, title="Acquirer timeout canary-90417")
    client = _app(session_factory, key_pair)

    # Act
    own = _search(client, key_pair, {"query": QUERY}, merchant_id=MERCHANT)
    others = _search(client, key_pair, {"query": QUERY}, merchant_id=OTHER_MERCHANT)

    # Assert
    assert sorted(str(i) for i in _result_ids(own)) == ["INC-OWN", "INC-PLATFORM"]
    assert sorted(str(i) for i in _result_ids(others)) == ["INC-OTHER", "INC-PLATFORM"]
    assert "canary-90417" not in str(own)


def test_incident_search_takes_the_scope_from_the_token_not_from_a_body_field(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-OTHER", OTHER_MERCHANT, title="Acquirer timeout canary-90417")
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(
        SEARCH_ROUTE,
        json={"query": QUERY, "merchant_id": str(OTHER_MERCHANT)},
        headers=_bearer(_merchant_token(key_pair.private_pem, MERCHANT)),
    )

    # Assert: either rejected or answered from the token's scope; never the other merchant's incident
    assert response.status_code in (200, 422)
    assert "canary-90417" not in response.text
    assert "INC-OTHER" not in response.text


def test_incident_search_returns_five_results_by_default(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    for number in range(7):
        _seed_incident(session_factory, f"INC-{number}", MERCHANT)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY})

    # Assert
    assert len(_as_list(body["results"])) == 5


def test_incident_search_respects_the_limit(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    for number in range(7):
        _seed_incident(session_factory, f"INC-{number}", MERCHANT)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY, "limit": 2})

    # Assert
    assert len(_as_list(body["results"])) == 2


@pytest.mark.parametrize("limit", [1, 20])
def test_incident_search_accepts_the_limit_bounds(
    session_factory: sessionmaker[Session], key_pair: KeyPair, limit: int
) -> None:
    # Arrange
    _seed_incident(session_factory, "INC-1", MERCHANT)
    client = _app(session_factory, key_pair)

    # Act
    body = _search(client, key_pair, {"query": QUERY, "limit": limit})

    # Assert
    assert _result_ids(body) == ["INC-1"]


# --- POST /v1/incidents/search: validation and auth --------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"query": ""},
        {"query": "x" * 501},
        {"query": 123},
        {"query": None},
        {"query": QUERY, "limit": 0},
        {"query": QUERY, "limit": 21},
        {"query": QUERY, "limit": -1},
        {"query": QUERY, "limit": 1.5},
        {"query": QUERY, "limit": "many"},
    ],
    ids=[
        "no_query",
        "empty_query",
        "query_501_chars",
        "query_not_a_string",
        "query_null",
        "limit_0",
        "limit_21",
        "limit_negative",
        "limit_fractional",
        "limit_not_a_number",
    ],
)
def test_incident_search_rejects_an_invalid_body_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, body: object
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(SEARCH_ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize("query", ["a", "x" * 500], ids=["one_character", "500_characters"])
def test_incident_search_accepts_the_query_length_bounds(
    session_factory: sessionmaker[Session], key_pair: KeyPair, query: str
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(
        SEARCH_ROUTE, json={"query": query}, headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.json() == {"results": []}


# --- POST /v1/bulk/analyze: shape and values ---------------------------------------------------------------


def test_bulk_analyze_returns_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    body = _bulk(client, key_pair)

    # Assert
    assert set(body) == BULK_KEYS


def test_bulk_analyze_reports_the_token_arithmetic_of_the_merchants_window(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange: 30 rows of 32 tokens plus the 2-token header; 960 tokens fit one chunk of 2000
    client = _app(session_factory, key_pair)

    # Act
    body = _bulk(client, key_pair)

    # Assert
    prompt = str(body["prompt"])
    assert (body["transactions"], body["chunks"], body["naive_tokens"]) == (
        OWN_ROWS,
        1,
        2 + OWN_ROWS * ROW_TOKENS,
    )
    assert body["optimized_tokens"] == count_tokens(prompt)
    assert body["within_budget"] is True
    assert float(str(body["reduction"])) == pytest.approx(
        1 - count_tokens(prompt) / (2 + OWN_ROWS * ROW_TOKENS), abs=TOLERANCE
    )
    assert float(str(body["reduction"])) > 0.0


def test_bulk_analyze_narrates_with_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    body = _bulk(client, key_pair)

    # Assert
    assert (body["mode"], body["text"], body["model"]) == ("llm", NARRATION, MODEL)


def test_bulk_analyze_gives_the_llm_the_summary_only_never_a_payment_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _app(session_factory, key_pair, llm)

    # Act
    body = _bulk(client, key_pair)

    # Assert
    (request,) = llm.calls
    assert [(item.ref, item.summary) for item in request.evidence] == [("bulk:summary", str(body["prompt"]))]
    every_id = [*payments.own_ids, *payments.others_ids, payments.at_end_id]
    assert [i for i in every_id if str(i) in repr(request) or str(i) in str(body)] == []


def test_bulk_analyze_counts_only_the_half_open_window_of_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange: the merchant also has a row exactly at `end` (excluded); another merchant has 5 rows inside
    client = _app(session_factory, key_pair)

    # Act
    own = _bulk(client, key_pair, merchant_id=MERCHANT)
    others = _bulk(client, key_pair, merchant_id=OTHER_MERCHANT)

    # Assert
    assert (own["transactions"], others["transactions"]) == (OWN_ROWS, OTHER_ROWS)
    assert others["naive_tokens"] == 2 + OTHER_ROWS * ROW_TOKENS


def test_bulk_analyze_takes_the_scope_from_the_token_not_from_a_body_field(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(
        BULK_ROUTE,
        json=_bulk_body(merchant_id=str(OTHER_MERCHANT)),
        headers=_bearer(_merchant_token(key_pair.private_pem, MERCHANT)),
    )

    # Assert: either rejected or answered from the token's scope with the merchant's own 30 rows
    assert response.status_code in (200, 422)
    if response.status_code == 200:
        assert response.json()["transactions"] == OWN_ROWS


def test_bulk_analyze_of_a_merchant_without_payments_returns_zeroes(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    body = _bulk(client, key_pair, merchant_id=EMPTY_MERCHANT)

    # Assert
    assert (body["transactions"], body["chunks"], body["reduction"]) == (0, 0, 0.0)


def test_bulk_analyze_uses_the_token_budget_to_shrink_the_prompt(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    tight = _bulk(client, key_pair, _bulk_body(token_budget=100))
    loose = _bulk(client, key_pair, _bulk_body(token_budget=4000))

    # Assert
    assert int(str(tight["optimized_tokens"])) <= int(str(loose["optimized_tokens"]))
    assert tight["within_budget"] is (int(str(tight["optimized_tokens"])) <= 100)
    assert loose["within_budget"] is True


def test_bulk_analyze_with_an_unavailable_llm_returns_t2_fallback_with_the_same_analysis(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    with_llm = _bulk(_app(session_factory, key_pair), key_pair)
    client = _app(session_factory, key_pair, DownLlm())

    # Act
    body = _bulk(client, key_pair)

    # Assert
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    assert {k: v for k, v in body.items() if k not in ("mode", "text", "model")} == {
        k: v for k, v in with_llm.items() if k not in ("mode", "text", "model")
    }


def test_bulk_analyze_sends_cache_control_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(
        BULK_ROUTE, json=_bulk_body(), headers=_bearer(_merchant_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


# --- POST /v1/bulk/analyze: validation ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        _bulk_body(end=WINDOW_START.isoformat()),
        _bulk_body(end=(WINDOW_START - timedelta(seconds=1)).isoformat()),
        _bulk_body(
            start=(WINDOW_END - timedelta(days=31, seconds=1)).isoformat(), end=WINDOW_END.isoformat()
        ),
        _bulk_body(start=(WINDOW_END - timedelta(days=365)).isoformat(), end=WINDOW_END.isoformat()),
        {"end": WINDOW_END.isoformat()},
        {"start": WINDOW_START.isoformat()},
        _bulk_body(start="not-a-date"),
        _bulk_body(end=None),
        _bulk_body(token_budget=99),
        _bulk_body(token_budget=0),
        _bulk_body(token_budget=4001),
        _bulk_body(token_budget="lots"),
        _bulk_body(token_budget=800.5),
    ],
    ids=[
        "end_equals_start",
        "end_before_start",
        "span_31_days_and_1_second",
        "span_a_year",
        "no_start",
        "no_end",
        "start_not_a_date",
        "end_null",
        "budget_99",
        "budget_0",
        "budget_4001",
        "budget_not_a_number",
        "budget_fractional",
    ],
)
def test_bulk_analyze_rejects_an_invalid_body_with_422_before_calling_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments, body: object
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _app(session_factory, key_pair, llm)

    # Act
    response = client.post(BULK_ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 422
    assert llm.calls == []


@pytest.mark.parametrize(
    "body",
    [
        _bulk_body(start=(WINDOW_END - timedelta(days=31)).isoformat(), end=WINDOW_END.isoformat()),
        _bulk_body(token_budget=100),
        _bulk_body(token_budget=4000),
        _bulk_body(start=(WINDOW_START).isoformat(), end=(WINDOW_START + timedelta(seconds=1)).isoformat()),
    ],
    ids=["span_exactly_31_days", "budget_100", "budget_4000", "span_1_second"],
)
def test_bulk_analyze_accepts_the_span_and_budget_bounds(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments: SeededPayments, body: object
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(BULK_ROUTE, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 200


# --- auth (both routes) ------------------------------------------------------------------------------------

ROUTE_BODIES = [(SEARCH_ROUTE, {"query": QUERY}), (BULK_ROUTE, _bulk_body())]
ROUTE_IDS = ["incident_search", "bulk_analyze"]


@pytest.mark.parametrize(("route", "body"), ROUTE_BODIES, ids=ROUTE_IDS)
def test_a_user_token_is_rejected_with_403_forbidden_and_never_reaches_the_llm(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str, body: object
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _app(session_factory, key_pair, llm)

    # Act
    response = client.post(route, json=body, headers=_bearer(_user_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


@pytest.mark.parametrize(("route", "body"), ROUTE_BODIES, ids=ROUTE_IDS)
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer garbage-token-canary-8842"}, {"Authorization": "Basic Z2FyYmFnZQ=="}],
    ids=["no_authorization_header", "garbage_token", "not_a_bearer_scheme"],
)
def test_a_missing_or_malformed_token_is_rejected_with_401_invalid_token(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    route: str,
    body: object,
    headers: dict[str, str],
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _app(session_factory, key_pair, llm)

    # Act
    response = client.post(route, json=body, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert "garbage-token-canary-8842" not in response.text
    assert llm.calls == []


@pytest.mark.parametrize(("route", "body"), ROUTE_BODIES, ids=ROUTE_IDS)
def test_a_merchant_token_signed_by_another_key_is_rejected_with_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    other_key_pair: KeyPair,
    route: str,
    body: object,
) -> None:
    # Arrange
    client = _app(session_factory, key_pair)

    # Act
    response = client.post(route, json=body, headers=_bearer(_merchant_token(other_key_pair.private_pem)))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize(("route", "body"), ROUTE_BODIES, ids=ROUTE_IDS)
def test_an_expired_merchant_token_is_rejected_with_401_once_the_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, route: str, body: object
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW + timedelta(seconds=901), key_pair.public_pem, RecordingLlm()
    )

    # Act
    response = client.post(route, json=body, headers=_bearer(_merchant_token(key_pair.private_pem)))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
