"""Ops staff on POST /v1/assistant/failures: an explicit `merchant_id` query parameter, every other
principal as before (docs/specs/failure-tools.md section 11, ops principal)."""

import json
import logging
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
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, MockLlm
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.playbook import load_playbook
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

FAILURES_ROUTE = "/v1/assistant/failures"
QUERY_ROUTE = "/v1/assistant/query"
QUESTION = "Why are card payments failing this afternoon?"
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
OPS_USER = uuid.UUID("0d0d0d0d-0000-4000-8000-0000000000aa")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OPS_ROLES = ["ops_agent", "ops_lead"]
MERCHANT_ROLES = ["merchant_admin", "merchant_support"]


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
class CitingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return MockLlm().complete(request)


def _client(
    session_factory: sessionmaker[Session], public_pem: str, llm: CitingLlm, clock: Callable[[], datetime]
) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=llm,
        jwt_public_key_pem=public_pem,
        clock=clock,
        playbook=load_playbook(),
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


def _ops_token(private_pem: str, role: str = "ops_agent") -> str:
    claims = _claims(f"ops:{OPS_USER}", {"ops_user_id": str(OPS_USER)}, [role])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _merchant_token(
    private_pem: str, role: str = "merchant_support", merchant_id: uuid.UUID = MERCHANT
) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, [role])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _user_token(private_pem: str) -> str:
    claims = _claims(f"user:{USER}", {"user_id": str(USER)}, ["consumer"])
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _post(
    client: TestClient, token: str, params: dict[str, str] | list[tuple[str, str]] | None = None
) -> tuple[int, str, dict[str, object]]:
    response = client.post(FAILURES_ROUTE, params=params, json={"question": QUESTION}, headers=_bearer(token))
    parsed: dict[str, object] = response.json()
    return response.status_code, response.headers.get("Cache-Control", ""), parsed


def _seed_failures(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, card_bin: str
) -> list[str]:
    """A baseline of successes and, in the last two hours, eight issuer_unavailable failures on `card_bin`."""
    failed: list[str] = []
    with session_factory.begin() as session:
        for index in range(60):
            session.add(
                _fact(merchant_id, TEST_NOW - timedelta(days=3, minutes=index * 30), "succeeded", None)
            )
        for minute in range(8):
            fact = _fact(
                merchant_id, TEST_NOW - timedelta(minutes=100 - minute), "declined", "issuer_unavailable"
            )
            fact.card_bin = card_bin
            session.add(fact)
            failed.append(str(fact.payment_id))
    return failed


def _fact(merchant_id: uuid.UUID, occurred_at: datetime, state: str, decline_code: str | None) -> PaymentFact:
    return PaymentFact(
        payment_id=uuid.uuid4(),
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state=state,
        provider="acq_a",
        decline_code=decline_code,
        card_bin="411111",
        entity_version=1,
        occurred_at=occurred_at,
    )


# --- ops staff with an explicit merchant ------------------------------------------------------------------


@pytest.mark.parametrize("role", OPS_ROLES)
def test_ops_staff_get_the_named_merchants_failures(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str
) -> None:
    # Arrange
    own = _seed_failures(session_factory, MERCHANT, "400000")
    others = _seed_failures(session_factory, OTHER_MERCHANT, "522222")
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    status, cache_control, body = _post(
        client, _ops_token(key_pair.private_pem, role), {"merchant_id": str(OTHER_MERCHANT)}
    )

    # Assert
    text_out = json.dumps(body)
    assert status == 200
    assert cache_control == "no-store"
    assert f"payment:{others[-1]}" in text_out
    assert [payment for payment in own if payment in text_out] == []


def test_ops_staff_get_exactly_the_body_the_merchant_itself_gets(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_failures(session_factory, MERCHANT, "400000")
    _seed_failures(session_factory, OTHER_MERCHANT, "522222")
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    merchant_status, _, merchant_body = _post(client, _merchant_token(key_pair.private_pem))
    ops_status, _, ops_body = _post(client, _ops_token(key_pair.private_pem), {"merchant_id": str(MERCHANT)})

    # Assert
    assert (merchant_status, ops_status) == (200, 200)
    assert ops_body == merchant_body


def test_ops_staff_see_the_same_masking_as_the_merchant_path(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    _seed_failures(session_factory, MERCHANT, "400000")
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    status, _, body = _post(client, _ops_token(key_pair.private_pem), {"merchant_id": str(MERCHANT)})

    # Assert
    query = body["query"]
    reasons = body["reasons"]
    assert status == 200
    assert isinstance(query, dict) and isinstance(reasons, list)
    assert "merchant_id" not in query
    assert [sorted(reason) for reason in reasons if "raw_codes" in reason] == []
    assert str(MERCHANT) not in json.dumps(body)


def test_ops_staff_logs_never_contain_the_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _seed_failures(session_factory, MERCHANT, "400000")
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)
    token = _ops_token(key_pair.private_pem)
    caplog.set_level(logging.DEBUG)

    # Act
    status, _, _ = _post(client, token, {"merchant_id": str(MERCHANT)})

    # Assert
    assert status == 200
    assert token not in caplog.text
    assert token.split(".")[2] not in caplog.text


# --- ops staff must name the merchant ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        None,
        {"merchant_id": ""},
        {"merchant_id": "not-a-uuid"},
        {"merchant_id": "all"},
        {"merchant_ids": str(MERCHANT)},
        {"merchant_id": "{" + str(MERCHANT) + "}"},
        {"merchant_id": "urn:uuid:" + str(MERCHANT)},
        {"merchant_id": MERCHANT.hex},
        {"merchant_id": str(MERCHANT).upper()},
    ],
    ids=["missing", "empty", "not_a_uuid", "all", "misspelled", "braces", "urn", "no_hyphens", "upper_case"],
)
def test_ops_staff_without_a_valid_merchant_id_get_422_before_any_work(
    session_factory: sessionmaker[Session], key_pair: KeyPair, params: dict[str, str] | None
) -> None:
    # Arrange
    _seed_failures(session_factory, MERCHANT, "400000")
    llm = CitingLlm()
    client = _client(session_factory, key_pair.public_pem, llm, lambda: TEST_NOW)

    # Act
    status, cache_control, body = _post(client, _ops_token(key_pair.private_pem), params)

    # Assert
    assert status == 422
    assert cache_control == "no-store"
    assert body == {"detail": "merchant_id is required for ops staff"}
    assert llm.calls == []


def test_ops_staff_naming_two_merchants_get_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    llm = CitingLlm()
    client = _client(session_factory, key_pair.public_pem, llm, lambda: TEST_NOW)
    params = [("merchant_id", str(MERCHANT)), ("merchant_id", str(OTHER_MERCHANT))]

    # Act
    status, _, body = _post(client, _ops_token(key_pair.private_pem), params)

    # Assert
    assert status == 422
    assert body == {"detail": "merchant_id is required for ops staff"}
    assert llm.calls == []


def test_ops_staff_cannot_put_the_merchant_in_the_body(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    llm = CitingLlm()
    client = _client(session_factory, key_pair.public_pem, llm, lambda: TEST_NOW)

    # Act
    response = client.post(
        FAILURES_ROUTE,
        json={"question": QUESTION, "merchant_id": str(MERCHANT)},
        headers=_bearer(_ops_token(key_pair.private_pem)),
    )

    # Assert
    assert response.status_code == 422
    assert llm.calls == []


# --- merchant principals keep today's scope ---------------------------------------------------------------


@pytest.mark.parametrize("role", MERCHANT_ROLES)
@pytest.mark.parametrize("named", [OTHER_MERCHANT, MERCHANT], ids=["other_merchant", "own_merchant"])
def test_merchant_tokens_cannot_name_a_merchant(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str, named: uuid.UUID
) -> None:
    # Arrange
    _seed_failures(session_factory, OTHER_MERCHANT, "522222")
    llm = CitingLlm()
    client = _client(session_factory, key_pair.public_pem, llm, lambda: TEST_NOW)

    # Act
    status, cache_control, body = _post(
        client, _merchant_token(key_pair.private_pem, role), {"merchant_id": str(named)}
    )

    # Assert
    assert status == 422
    assert cache_control == "no-store"
    assert body == {"detail": "merchant_id is taken from the token"}
    assert llm.calls == []


@pytest.mark.parametrize("role", MERCHANT_ROLES)
def test_merchant_tokens_still_get_only_their_own_failures(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str
) -> None:
    # Arrange
    own = _seed_failures(session_factory, MERCHANT, "400000")
    others = _seed_failures(session_factory, OTHER_MERCHANT, "522222")
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    status, _, body = _post(client, _merchant_token(key_pair.private_pem, role))

    # Assert
    text_out = json.dumps(body)
    assert status == 200
    assert f"payment:{own[-1]}" in text_out
    assert [payment for payment in others if payment in text_out] == []


# --- 401 and 403 stay as they are, and come before 422 ----------------------------------------------------


@pytest.mark.parametrize("params", [None, {"merchant_id": str(MERCHANT)}], ids=["no_merchant", "merchant"])
def test_expired_ops_tokens_get_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, params: dict[str, str] | None
) -> None:
    # Arrange
    llm = CitingLlm()
    client = _client(session_factory, key_pair.public_pem, llm, lambda: TEST_NOW + timedelta(seconds=901))

    # Act
    status, _, body = _post(client, _ops_token(key_pair.private_pem), params)

    # Assert
    assert status == 401
    assert body == INVALID_TOKEN_BODY
    assert llm.calls == []


def test_ops_tokens_signed_by_another_key_get_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, other_key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    status, _, body = _post(client, _ops_token(other_key_pair.private_pem), {"merchant_id": str(MERCHANT)})

    # Assert
    assert status == 401
    assert body == INVALID_TOKEN_BODY


@pytest.mark.parametrize("role", ["ops_admin", "support", "ops"])
def test_ops_tokens_with_an_unknown_role_get_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    status, _, body = _post(client, _ops_token(key_pair.private_pem, role), {"merchant_id": str(MERCHANT)})

    # Assert
    assert status == 401
    assert body == INVALID_TOKEN_BODY


def test_a_garbage_token_gets_401(session_factory: sessionmaker[Session], key_pair: KeyPair) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    status, _, body = _post(client, "not.a.token", {"merchant_id": str(MERCHANT)})

    # Assert
    assert status == 401
    assert body == INVALID_TOKEN_BODY


@pytest.mark.parametrize("params", [None, {"merchant_id": str(MERCHANT)}], ids=["no_merchant", "merchant"])
def test_user_tokens_stay_403(
    session_factory: sessionmaker[Session], key_pair: KeyPair, params: dict[str, str] | None
) -> None:
    # Arrange
    _seed_failures(session_factory, MERCHANT, "400000")
    llm = CitingLlm()
    client = _client(session_factory, key_pair.public_pem, llm, lambda: TEST_NOW)

    # Act
    status, _, body = _post(client, _user_token(key_pair.private_pem), params)

    # Assert
    assert status == 403
    assert body == FORBIDDEN_BODY
    assert llm.calls == []


def test_ops_tokens_stay_403_on_the_query_route(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair.public_pem, CitingLlm(), lambda: TEST_NOW)

    # Act
    response = client.post(
        QUERY_ROUTE,
        params={"merchant_id": str(MERCHANT)},
        json={"question": QUESTION},
        headers=_bearer(_ops_token(key_pair.private_pem)),
    )

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
