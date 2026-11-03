"""Tests for POST /v1/assistant/explain-settlement, written from docs/specs/sq4.md section 6."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmResponse, LlmUnavailableError
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

ROUTE = "/v1/assistant/explain-settlement"
QUESTION = "Why is my October payout lower than expected?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
NOT_FOUND_BODY = {"detail": "settlement not found"}
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("1c2d7b63-4a2f-4d66-8e4b-7a8f3b2c0d02")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
CAPTURED_AT = datetime(2026, 10, 14, 9, 30, tzinfo=UTC)
TOP_LEVEL_KEYS = {
    "period",
    "currency",
    "contract_version",
    "fx_rate_id",
    "lines",
    "gross_minor",
    "expected_minor",
    "net_minor",
    "gap_minor",
    "verified",
    "mismatches",
    "projection_checked",
    "summary",
    "mode",
    "text",
    "model",
    "tool_calls",
    "evidence",
}
LINE_KEYS = {"kind", "amount_minor", "count"}

INSERT_ENTRY = text(
    "INSERT INTO ops_ai.settlement_entries (payment_id, merchant_id, amount_minor, currency, captured_at, "
    "statement_period, refunded, chargeback, ingest_source) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, :captured_at, :statement_period, :refunded, "
    ":chargeback, 'seed')"
)
INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, :currency, 'card', 'succeeded', 1, :occurred_at)"
)

# USD cents after conversion: 10000, 10870, 6700, 5000, 2174, 1190
MIXED: tuple[tuple[int, str, bool, bool], ...] = (
    (10_000, "USD", False, False),
    (10_000, "EUR", False, False),
    (10_000, "JPY", True, False),
    (5_000, "USD", False, True),
    (2_000, "EUR", True, True),
    (100_000, "INR", True, False),
)
MIXED_LINES = [
    {"kind": "gross", "amount_minor": 35_934, "count": 6},
    {"kind": "mdr", "amount_minor": -1_042, "count": 6},
    {"kind": "fixed_fees", "amount_minor": -180, "count": 6},
    {"kind": "fx", "amount_minor": -419, "count": 4},
    {"kind": "refunds", "amount_minor": -7_890, "count": 2},
    {"kind": "chargebacks", "amount_minor": -7_174, "count": 2},
    {"kind": "chargeback_fees", "amount_minor": -3_000, "count": 2},
    {"kind": "reserve", "amount_minor": -1_797, "count": 6},
]
MIXED_SUMMARY = (
    "Your 2026-10 payout is 144.32 USD, 204.60 USD less than the 348.92 USD you would expect from gross "
    "sales of 359.34 USD minus the 2.90% MDR. "
    "Fixed fees: 1.80 USD (6 payments at 0.30 USD each). "
    "FX conversion: 4.19 USD on 4 non-USD payments (rate set fx-2026-10). "
    "Refunds: 78.90 USD on 2 payments. "
    "Chargebacks: 71.74 USD on 2 payments. "
    "Chargeback fees: 30.00 USD (2 at 15.00 USD each). "
    "Rolling reserve: 17.97 USD held (5.00% of gross), released in a later payout."
)
MIXED_REFS = [
    "statement:2026-10",
    "line:fixed_fees",
    "line:fx",
    "line:refunds",
    "line:chargebacks",
    "line:chargeback_fees",
    "line:reserve",
    "contract:std-2026-10",
    "fx:fx-2026-10",
    "crosscheck:2026-10",
]


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


def _load(
    session_factory: sessionmaker[Session],
    rows: tuple[tuple[int, str, bool, bool], ...],
    *,
    merchant_id: uuid.UUID = MERCHANT,
    period: str = "2026-10",
    fact_amount_offset: int = 0,
) -> list[uuid.UUID]:
    payment_ids: list[uuid.UUID] = []
    with session_factory.begin() as session:
        for offset, (amount_minor, currency, refunded, chargeback) in enumerate(rows):
            payment_id = uuid.uuid4()
            captured_at = CAPTURED_AT + timedelta(minutes=offset)
            session.execute(
                INSERT_ENTRY,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": amount_minor,
                    "currency": currency,
                    "captured_at": captured_at,
                    "statement_period": period,
                    "refunded": refunded,
                    "chargeback": chargeback,
                },
            )
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": amount_minor + (fact_amount_offset if offset == 0 else 0),
                    "currency": currency,
                    "occurred_at": captured_at,
                },
            )
            payment_ids.append(payment_id)
    return payment_ids


@pytest.fixture
def seeded(session_factory: sessionmaker[Session]) -> None:
    """The merchant has the mixed October statement and a one-payment September one; another merchant has a
    newer, different October statement of its own."""
    _load(session_factory, MIXED)
    _load(session_factory, ((20_000, "USD", False, False),), period="2026-09")
    _load(session_factory, ((777_700, "USD", False, True),), merchant_id=OTHER_MERCHANT)


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


def _post(
    client: TestClient,
    key_pair: KeyPair,
    body: dict[str, object] | None = None,
    merchant_id: uuid.UUID = MERCHANT,
) -> Response:
    response: Response = client.post(
        ROUTE,
        json=body if body is not None else {"question": QUESTION},
        headers=_bearer(_merchant_token(key_pair.private_pem, merchant_id)),
    )
    return response


def _post_ok(
    client: TestClient,
    key_pair: KeyPair,
    body: dict[str, object] | None = None,
    merchant_id: uuid.UUID = MERCHANT,
) -> dict[str, object]:
    response = _post(client, key_pair, body, merchant_id)
    assert response.status_code == 200
    parsed: dict[str, object] = response.json()
    return parsed


# --- success shape ---------------------------------------------------------------------------------------


def test_explain_settlement_returns_exactly_the_specified_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert set(body) == TOP_LEVEL_KEYS
    lines = body["lines"]
    assert isinstance(lines, list)
    assert all(set(line) == LINE_KEYS for line in lines)
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert all(set(item) == {"ref", "summary"} for item in evidence)


def test_explain_settlement_returns_the_latest_verified_statement_of_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert {k: body[k] for k in TOP_LEVEL_KEYS - {"evidence"}} == {
        "period": "2026-10",
        "currency": "USD",
        "contract_version": "std-2026-10",
        "fx_rate_id": "fx-2026-10",
        "lines": MIXED_LINES,
        "gross_minor": 35_934,
        "expected_minor": 34_892,
        "net_minor": 14_432,
        "gap_minor": 20_460,
        "verified": True,
        "mismatches": [],
        "projection_checked": 6,
        "summary": MIXED_SUMMARY,
        "mode": "llm",
        "text": NARRATION,
        "model": MODEL,
        "tool_calls": [{"name": "explain_settlement", "args": {"period": "latest"}}],
    }


def test_explain_settlement_lists_the_specified_evidence_refs_in_order(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert [item["ref"] for item in evidence] == MIXED_REFS


def test_explain_settlement_sets_cache_control_no_store_on_a_200(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, key_pair)

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_explain_settlement_with_a_period_returns_that_period_and_records_it_in_the_tool_call(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair, {"question": QUESTION, "period": "2026-09"})

    # Assert
    assert body["period"] == "2026-09"
    assert body["gross_minor"] == 20_000
    assert body["tool_calls"] == [{"name": "explain_settlement", "args": {"period": "2026-09"}}]


def test_explain_settlement_scopes_the_statement_to_the_token_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    body = _post_ok(client, key_pair, merchant_id=OTHER_MERCHANT)

    # Assert
    assert body["gross_minor"] == 777_700
    assert body["projection_checked"] == 1
    assert "359.34" not in str(body)


def test_explain_settlement_with_an_unavailable_llm_returns_200_t2_fallback_with_the_same_statement(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    with_llm = _post_ok(
        _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm()), key_pair
    )
    down = DownLlm()
    down_client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, down)

    # Act
    fallback = _post_ok(down_client, key_pair)

    # Assert
    assert (fallback["mode"], fallback["text"], fallback["model"]) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert {k: v for k, v in fallback.items() if k not in ("mode", "text", "model")} == {
        k: v for k, v in with_llm.items() if k not in ("mode", "text", "model")
    }


def test_explain_settlement_returns_an_unverified_statement_with_its_mismatches_and_no_llm_call(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _load(session_factory, MIXED, fact_amount_offset=1)
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    body = _post_ok(client, key_pair)

    # Assert
    assert (body["verified"], body["mismatches"]) == (False, ["projection_mismatch"])
    assert (body["mode"], body["text"], body["model"]) == ("t2_fallback", None, None)
    assert body["summary"] == (
        "The 2026-10 settlement could not be verified (projection_mismatch); "
        "contact support with reference 2026-10."
    )
    evidence = body["evidence"]
    assert isinstance(evidence, list)
    assert [item["ref"] for item in evidence] == ["statement:2026-10", "crosscheck:2026-10"]
    assert llm.calls == []


# --- not found -------------------------------------------------------------------------------------------


def test_explain_settlement_returns_404_when_the_merchant_has_no_entries(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)
    stranger = uuid.UUID("2d3e8c74-5b30-4e77-9f5c-8b9a4c3d1e03")

    # Act
    response = _post(client, key_pair, merchant_id=stranger)

    # Assert
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    assert llm.calls == []


def test_explain_settlement_returns_404_for_a_period_without_entries(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = _post(client, key_pair, {"question": QUESTION, "period": "2026-08"})

    # Assert
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY
    assert llm.calls == []


@pytest.mark.parametrize("period", ["2026-01", "2026-12"])
def test_explain_settlement_accepts_the_first_and_last_month_of_a_year(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None, period: str
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, key_pair, {"question": QUESTION, "period": period})

    # Assert
    assert response.status_code == 404
    assert response.json() == NOT_FOUND_BODY


def test_explain_settlement_gives_one_404_for_a_period_only_another_merchant_has_and_one_nobody_has(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    _load(session_factory, ((1_000, "USD", False, False),), merchant_id=OTHER_MERCHANT, period="2026-07")
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    others_period = _post(client, key_pair, {"question": QUESTION, "period": "2026-07"})
    nobodys_period = _post(client, key_pair, {"question": QUESTION, "period": "2026-06"})

    # Assert
    assert others_period.status_code == nobodys_period.status_code == 404
    assert others_period.content == nobodys_period.content


# --- who may call it -------------------------------------------------------------------------------------


def test_explain_settlement_rejects_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(
        ROUTE, json={"question": QUESTION}, headers=_bearer(_user_token(key_pair.private_pem))
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert llm.calls == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer garbage-token-canary-8842"}, {"Authorization": "Basic Z2FyYmFnZQ=="}],
    ids=["no_authorization_header", "garbage_token", "not_a_bearer_scheme"],
)
def test_explain_settlement_without_a_valid_token_returns_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None, headers: dict[str, str]
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = client.post(ROUTE, json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert "garbage-token-canary-8842" not in response.text
    assert llm.calls == []


def test_explain_settlement_rejects_a_merchant_token_signed_by_another_key_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, other_key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, other_key_pair)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


def test_explain_settlement_rejects_an_expired_merchant_token_once_the_injected_clock_passes_its_expiry(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(
        session_factory, lambda: TEST_NOW + timedelta(seconds=901), key_pair.public_pem, RecordingLlm()
    )

    # Act
    response = _post(client, key_pair)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


# --- body validation -------------------------------------------------------------------------------------


@pytest.mark.parametrize("field_name", ["merchant_id", "user_id", "currency", "limit"])
def test_explain_settlement_rejects_an_extra_field_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None, field_name: str
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = _post(client, key_pair, {"question": QUESTION, field_name: str(OTHER_MERCHANT)})

    # Assert
    assert response.status_code == 422
    assert llm.calls == []


@pytest.mark.parametrize(
    "period",
    [
        "2026-13",
        "2026-00",
        "2026-1",
        "26-10",
        "2026-10-01",
        "2026/10",
        "",
        "202610",
        "latest",
        " 2026-10",
        "2026-10 ",
        "2026-10\n",
        "\uff12\uff10\uff12\uff16-\uff11\uff10",
        202610,
        ["2026-10"],
    ],
    ids=[
        "month_13",
        "month_00",
        "month_one_digit",
        "two_digit_year",
        "full_date",
        "slash_separator",
        "empty",
        "no_separator",
        "the_word_latest",
        "leading_space",
        "trailing_space",
        "trailing_newline",
        "fullwidth_digits",
        "integer",
        "list",
    ],
)
def test_explain_settlement_rejects_a_period_that_is_not_yyyy_mm_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None, period: object
) -> None:
    # Arrange
    llm = RecordingLlm()
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, llm)

    # Act
    response = _post(client, key_pair, {"question": QUESTION, "period": period})

    # Assert
    assert response.status_code == 422
    assert llm.calls == []


def test_explain_settlement_does_not_echo_a_rejected_period(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, key_pair, {"question": QUESTION, "period": "canary-period-8842"})

    # Assert
    assert response.status_code == 422
    assert "canary-period-8842" not in response.text


@pytest.mark.parametrize(
    "body",
    [{}, {"question": ""}, {"question": "x" * 2001}, {"question": 123}, {"period": "2026-10"}],
    ids=["no_question", "empty_question", "question_2001_chars", "question_not_a_string", "period_only"],
)
def test_explain_settlement_rejects_an_invalid_question_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, key_pair, body)

    # Assert
    assert response.status_code == 422


def test_explain_settlement_accepts_a_question_of_exactly_2000_characters(
    session_factory: sessionmaker[Session], key_pair: KeyPair, seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, lambda: TEST_NOW, key_pair.public_pem, RecordingLlm())

    # Act
    response = _post(client, key_pair, {"question": "x" * 2000})

    # Assert
    assert response.status_code == 200
