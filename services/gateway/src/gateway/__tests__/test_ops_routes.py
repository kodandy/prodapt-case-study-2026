"""HTTP tests for the gateway ops routes and merchant support keys, from docs/specs/compliance-rbac.md
sections 1 to 3 (and the KYC and GDPR sections only where the ops routes expose them)."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import (
    authenticate_api_key_role,
    issue_api_key,
    sign_up,
)
from gateway.accounts.sessions import issue_session_token
from gateway.accounts.tokens import issue_merchant_token
from gateway.admin import (
    OpsEnrolment,
    OpsPrincipal,
    OpsRole,
    bootstrap_merchant,
    bootstrap_ops_user,
    issue_ops_session_token,
    verify_ops_session_token,
)
from gateway.app import GatewayAppDeps, create_app
from gateway.audit.service import record_audit
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

SESSIONS = "/v1/ops/sessions"
OPS_AI_TOKENS = "/v1/ops/ops-ai-tokens"
KYC_REVIEWS = "/v1/ops/kyc-reviews"
DATA_REQUESTS = "/v1/ops/data-requests"
AUDIT = "/v1/ops/audit"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
AGENT_EMAIL = "agent@ops.example.com"
LEAD_EMAIL = "lead@ops.example.com"
PASSWORD = "ops-correct-horse-battery-9Z"
WRONG_PASSWORD = "ops-wrong-horse-battery-1A"
STEP_SECONDS = 30
INVALID_OPS_SESSION = {"detail": "invalid ops session"}
INVALID_CREDENTIALS = {"detail": "invalid credentials"}
FORBIDDEN = {"detail": "forbidden"}
AUDIT_ITEM_KEYS = {
    "id",
    "actor_id",
    "actor_role",
    "action",
    "target",
    "summary",
    "correlation_id",
    "occurred_at",
}
KYC_ITEM_KEYS = {"review_id", "user_id", "status", "reasons", "opened_at", "resolved_at", "resolved_by"}
DSR_ITEM_KEYS = {"request_id", "user_id", "kind", "status", "received_at", "due_at", "completed_at"}
CHARGE_BODY = {
    "amount_minor": 1_000,
    "currency": "USD",
    "card": {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"},
}


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


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str | None,
    totp_key: str | None = TOTP_KEY,
) -> TestClient:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(
        charge=charge,
        api_key_pepper=TEST_API_KEY_PEPPER,
        jwt_private_key_pem=private_pem,
        totp_encryption_key=totp_key,
    )
    return TestClient(create_app(deps))


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _code(secret: str, clock: FixedClock, offset_steps: int = 0) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()) + offset_steps * STEP_SECONDS)


def _wrong_code(secret: str, clock: FixedClock) -> str:
    nearby = {_code(secret, clock, offset) for offset in range(-3, 4)}
    return next(candidate for candidate in (f"{n:06d}" for n in range(1, 1000)) if candidate not in nearby)


def _ops_user(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    role: OpsRole = "ops_agent",
    email: str = AGENT_EMAIL,
) -> OpsEnrolment:
    return bootstrap_ops_user(session_factory, clock, email, PASSWORD, role, TOTP_KEY)


def _login(
    client: TestClient,
    email: str,
    password: str,
    code: str,
) -> Response:
    response: Response = client.post(SESSIONS, json={"email": email, "password": password, "totp_code": code})
    return response


def _ops_session(
    client: TestClient,
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    role: OpsRole = "ops_agent",
) -> tuple[str, OpsEnrolment]:
    """Enrols an ops user of `role`, logs in over HTTP and returns the bearer session token."""
    email = AGENT_EMAIL if role == "ops_agent" else LEAD_EMAIL
    enrolment = _ops_user(session_factory, clock, role, email)
    response = _login(client, email, PASSWORD, _code(enrolment.totp_secret, clock))
    assert response.status_code == 201
    return str(response.json()["access_token"]), enrolment


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _record(session_factory: sessionmaker[Session], clock: FixedClock, action: str, target: str) -> None:
    with session_factory.begin() as session:
        record_audit(session, clock, "user:seed", "consumer", action, target, {"note": action})


def _ops_ai_claims(token: str, public_pem: str) -> dict[str, object]:
    """Verifies `token` as a gateway-issued ops-ai token (signature, issuer, audience); returns its claims."""
    claims: dict[str, object] = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        audience="ops-ai",
        issuer="gateway",
        options={"verify_exp": False, "verify_iat": False},
    )
    return claims


def _sign_up(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, "route-test-password-7Q")
        session.commit()
    return result.user_id


# --- POST /v1/ops/sessions ---------------------------------------------------------------------------------


def test_ops_login_returns_201_with_a_bearer_session_for_the_stored_role(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock, "ops_lead", LEAD_EMAIL)

    # Act
    response = _login(client, LEAD_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"access_token", "token_type", "expires_in", "role"}
    assert (body["token_type"], body["expires_in"], body["role"]) == ("Bearer", 900, "ops_lead")
    assert verify_ops_session_token(key_pair.private_pem, body["access_token"], clock()) == OpsPrincipal(
        ops_user_id=enrolment.ops_user_id, role="ops_lead"
    )


def test_ops_login_response_is_not_cacheable(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock)

    # Act
    response = _login(client, AGENT_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"


def test_ops_login_writes_an_ops_login_audit_row_for_the_ops_actor(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock)

    # Act
    _login(client, AGENT_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    rows = _rows(session_factory, "SELECT actor_id, actor_role, action FROM gateway.audit_log")
    assert rows == [
        {"actor_id": f"ops:{enrolment.ops_user_id}", "actor_role": "ops_agent", "action": "ops.login"}
    ]


@pytest.mark.parametrize("failure", ["unknown_email", "wrong_password", "wrong_code", "replayed_code"])
def test_ops_login_with_bad_credentials_returns_the_identical_401(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair, failure: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock)
    secret = enrolment.totp_secret
    email, password, code = AGENT_EMAIL, PASSWORD, _code(secret, clock)
    if failure == "unknown_email":
        email = "nobody@ops.example.com"
    elif failure == "wrong_password":
        password = WRONG_PASSWORD
    elif failure == "wrong_code":
        code = _wrong_code(secret, clock)
    else:
        assert _login(client, AGENT_EMAIL, PASSWORD, code).status_code == 201

    # Act
    response = _login(client, email, password, code)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_CREDENTIALS


def test_ops_login_without_a_totp_code_is_rejected_because_totp_is_mandatory(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    _ops_user(session_factory, clock)

    # Act
    response = client.post(SESSIONS, json={"email": AGENT_EMAIL, "password": PASSWORD})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert _rows(session_factory, "SELECT action FROM gateway.audit_log") == []


def test_ops_login_fifth_consecutive_failure_returns_429_with_retry_after(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock)
    code = _code(enrolment.totp_secret, clock)
    first_four = [_login(client, AGENT_EMAIL, WRONG_PASSWORD, code).status_code for _ in range(4)]

    # Act
    fifth = _login(client, AGENT_EMAIL, WRONG_PASSWORD, code)

    # Assert
    assert first_four == [401, 401, 401, 401]
    assert fifth.status_code == 429
    assert fifth.headers["Retry-After"] == "900"


def test_ops_login_while_locked_returns_429_even_with_the_correct_credentials(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock)
    for _ in range(5):
        _login(client, AGENT_EMAIL, WRONG_PASSWORD, _code(enrolment.totp_secret, clock))
    clock.advance(timedelta(seconds=60))

    # Act
    response = _login(client, AGENT_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "840"


def test_ops_login_succeeds_again_after_the_lock_has_passed(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    enrolment = _ops_user(session_factory, clock)
    for _ in range(5):
        _login(client, AGENT_EMAIL, WRONG_PASSWORD, _code(enrolment.totp_secret, clock))
    clock.advance(timedelta(minutes=15, seconds=1))

    # Act
    response = _login(client, AGENT_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    assert response.status_code == 201


def test_ops_login_without_a_jwt_signing_key_returns_503(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock, None)
    enrolment = _ops_user(session_factory, clock)

    # Act
    response = _login(client, AGENT_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    assert response.status_code == 503


def test_ops_login_without_a_totp_encryption_key_returns_503(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem, totp_key=None)
    enrolment = _ops_user(session_factory, clock)

    # Act
    response = _login(client, AGENT_EMAIL, PASSWORD, _code(enrolment.totp_secret, clock))

    # Assert
    assert response.status_code == 503


# --- ops session required on /v1/ops/* ---------------------------------------------------------------------


def _ops_routes() -> list[tuple[str, str, dict[str, object] | None]]:
    some_user = uuid.uuid4()
    return [
        ("POST", OPS_AI_TOKENS, None),
        ("GET", KYC_REVIEWS, None),
        ("POST", f"/v1/ops/users/{some_user}/kyc/verify", {"outcome": "verified"}),
        ("GET", DATA_REQUESTS, None),
        ("GET", AUDIT, None),
    ]


OPS_ROUTE_IDS = ["ops_ai_tokens", "kyc_reviews", "kyc_verify", "data_requests", "audit"]


@pytest.mark.parametrize(("method", "path", "body"), _ops_routes(), ids=OPS_ROUTE_IDS)
def test_ops_routes_without_a_token_return_401_invalid_ops_session(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)

    # Act
    response = client.request(method, path, json=body)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION


@pytest.mark.parametrize(("method", "path", "body"), _ops_routes(), ids=OPS_ROUTE_IDS)
def test_ops_routes_reject_a_consumer_session_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    user_id = _sign_up(session_factory, clock, "alice@example.com")
    token = issue_session_token(key_pair.private_pem, user_id, clock(), ("pwd",))

    # Act
    response = client.request(method, path, json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION


@pytest.mark.parametrize(("method", "path", "body"), _ops_routes(), ids=OPS_ROUTE_IDS)
def test_ops_routes_reject_a_merchant_api_key_and_a_merchant_token_with_401(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    key_pair: KeyPair,
    method: str,
    path: str,
    body: dict[str, object] | None,
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    credentials = bootstrap_merchant(session_factory, clock, "Acme", TEST_API_KEY_PEPPER)
    merchant_token = issue_merchant_token(key_pair.private_pem, credentials.merchant_id, clock())

    # Act
    by_key = client.request(method, path, json=body, headers=_bearer(credentials.api_key))
    by_token = client.request(method, path, json=body, headers=_bearer(merchant_token))

    # Assert
    assert (by_key.status_code, by_key.json()) == (401, INVALID_OPS_SESSION)
    assert (by_token.status_code, by_token.json()) == (401, INVALID_OPS_SESSION)


def test_ops_routes_reject_an_expired_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)
    clock.advance(timedelta(seconds=900))

    # Act
    response = client.get(KYC_REVIEWS, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION


def test_ops_routes_reject_a_session_for_an_ops_user_that_does_not_exist(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    ghost = OpsPrincipal(ops_user_id=uuid.uuid4(), role="ops_lead")
    token = issue_ops_session_token(key_pair.private_pem, ghost, clock())

    # Act
    response = client.get(AUDIT, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION


def test_ops_routes_reject_a_session_of_a_disabled_ops_user(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, enrolment = _ops_session(client, session_factory, clock)
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.ops_users SET disabled_at = :now WHERE id = :id"),
            {"now": clock(), "id": enrolment.ops_user_id},
        )

    # Act
    response = client.get(KYC_REVIEWS, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION


def test_ops_routes_reject_a_session_whose_role_differs_from_the_stored_role(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, enrolment = _ops_session(client, session_factory, clock, "ops_agent")
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.ops_users SET role = 'ops_lead' WHERE id = :id"),
            {"id": enrolment.ops_user_id},
        )

    # Act
    response = client.get(AUDIT, headers=_bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION


def test_an_ops_session_is_not_a_credential_on_merchant_or_consumer_routes(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, "ops_lead")

    # Act
    merchant_route = client.get("/v1/payments", headers=_bearer(token))
    consumer_route = client.get("/v1/me/wallets", headers=_bearer(token))

    # Assert
    assert (merchant_route.status_code, merchant_route.json()) == (401, {"detail": "invalid api key"})
    assert (consumer_route.status_code, consumer_route.json()) == (401, {"detail": "invalid session"})


# --- POST /v1/ops/ops-ai-tokens ----------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_ops_ai_token_route_issues_a_signed_ops_ai_token_for_the_ops_user(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair, role: OpsRole
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, enrolment = _ops_session(client, session_factory, clock, role)

    # Act
    response = client.post(OPS_AI_TOKENS, headers=_bearer(token))

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"access_token", "token_type", "expires_in"}
    assert (body["token_type"], body["expires_in"]) == ("Bearer", 900)
    claims = _ops_ai_claims(body["access_token"], key_pair.public_pem)
    issued_at = int(clock().timestamp())
    assert claims == {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"ops:{enrolment.ops_user_id}",
        "ops_user_id": str(enrolment.ops_user_id),
        "roles": [role],
        "iat": issued_at,
        "exp": issued_at + 900,
        "jti": claims.get("jti"),
    }


# --- GET /v1/ops/kyc-reviews and POST /v1/ops/users/{id}/kyc/verify ----------------------------------------


@pytest.mark.parametrize("role", ["ops_agent", "ops_lead"])
def test_kyc_review_list_is_empty_when_nothing_was_opened(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair, role: OpsRole
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, role)

    # Act
    response = client.get(KYC_REVIEWS, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_kyc_review_list_rejects_an_unknown_status_filter(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)

    # Act
    response = client.get(KYC_REVIEWS, params={"status": "bogus"}, headers=_bearer(token))

    # Assert
    assert response.status_code == 422


def _open_review(session_factory: sessionmaker[Session], clock: FixedClock, user_id: uuid.UUID) -> None:
    from gateway.compliance.kyc import OperationRequest, screen_operation

    screen_operation(
        session_factory,
        clock,
        user_id,
        OperationRequest(kind="transfer", amount_minor=1_000_000, currency="USD"),
    )


def test_kyc_review_list_returns_open_reviews_newest_first_with_the_spec_keys(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)
    first = _sign_up(session_factory, clock, "first@example.com")
    _open_review(session_factory, clock, first)
    opened_second = clock() + timedelta(minutes=5)
    clock.advance(timedelta(minutes=5))
    second = _sign_up(session_factory, clock, "second@example.com")
    _open_review(session_factory, clock, second)

    # Act
    response = client.get(KYC_REVIEWS, params={"status": "open"}, headers=_bearer(token))

    # Assert
    items = response.json()["items"]
    assert response.status_code == 200
    assert [item["user_id"] for item in items] == [str(second), str(first)]
    assert set(items[0]) == KYC_ITEM_KEYS
    assert (items[0]["status"], items[0]["reasons"]) == ("open", ["amount_threshold"])
    assert datetime.fromisoformat(items[0]["opened_at"]) == opened_second
    assert (items[0]["resolved_at"], items[0]["resolved_by"]) == (None, None)


def test_kyc_review_list_filters_by_status(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)
    user_id = _sign_up(session_factory, clock, "first@example.com")
    _open_review(session_factory, clock, user_id)

    # Act
    verified = client.get(KYC_REVIEWS, params={"status": "verified"}, headers=_bearer(token))
    rejected = client.get(KYC_REVIEWS, params={"status": "rejected"}, headers=_bearer(token))

    # Assert
    assert verified.json() == {"items": []}
    assert rejected.json() == {"items": []}


def test_kyc_verify_marks_the_user_verified_with_a_one_year_expiry_and_resolves_the_review(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, enrolment = _ops_session(client, session_factory, clock)
    user_id = _sign_up(session_factory, clock, "first@example.com")
    _open_review(session_factory, clock, user_id)

    # Act
    response = client.post(
        f"/v1/ops/users/{user_id}/kyc/verify", json={"outcome": "verified"}, headers=_bearer(token)
    )

    # Assert
    body = response.json()
    resolved = client.get(KYC_REVIEWS, params={"status": "verified"}, headers=_bearer(token)).json()["items"]
    assert response.status_code == 200
    assert set(body) == {"user_id", "kyc_status", "kyc_expires_at"}
    assert (body["user_id"], body["kyc_status"]) == (str(user_id), "verified")
    assert datetime.fromisoformat(body["kyc_expires_at"]) == clock() + timedelta(days=365)
    assert [(item["user_id"], item["resolved_by"]) for item in resolved] == [
        (str(user_id), f"ops:{enrolment.ops_user_id}")
    ]


def test_kyc_verify_with_a_rejected_outcome_marks_the_user_rejected(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)
    user_id = _sign_up(session_factory, clock, "first@example.com")

    # Act
    response = client.post(
        f"/v1/ops/users/{user_id}/kyc/verify", json={"outcome": "rejected"}, headers=_bearer(token)
    )

    # Assert
    stored = _rows(session_factory, "SELECT kyc_status FROM gateway.users WHERE id = :id", id=user_id)
    assert response.status_code == 200
    assert (response.json()["user_id"], response.json()["kyc_status"]) == (str(user_id), "rejected")
    assert stored == [{"kyc_status": "rejected"}]


def test_kyc_verify_audits_the_ops_actor(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, enrolment = _ops_session(client, session_factory, clock, "ops_lead")
    user_id = _sign_up(session_factory, clock, "first@example.com")

    # Act
    client.post(f"/v1/ops/users/{user_id}/kyc/verify", json={"outcome": "verified"}, headers=_bearer(token))

    # Assert
    rows = _rows(
        session_factory,
        "SELECT actor_id, actor_role FROM gateway.audit_log WHERE action = 'kyc.reverified'",
    )
    assert rows == [{"actor_id": f"ops:{enrolment.ops_user_id}", "actor_role": "ops_lead"}]


def test_kyc_verify_for_an_unknown_user_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)

    # Act
    response = client.post(
        f"/v1/ops/users/{uuid.uuid4()}/kyc/verify", json={"outcome": "verified"}, headers=_bearer(token)
    )

    # Assert
    assert response.status_code == 404


INVALID_VERIFY_BODIES = [{"outcome": "pending"}, {"outcome": ""}, {}, {"outcome": "verified", "x": 1}]


@pytest.mark.parametrize("body", INVALID_VERIFY_BODIES)
def test_kyc_verify_rejects_an_invalid_body(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)
    user_id = _sign_up(session_factory, clock, "first@example.com")

    # Act
    response = client.post(f"/v1/ops/users/{user_id}/kyc/verify", json=body, headers=_bearer(token))

    # Assert
    assert response.status_code == 422


def test_kyc_verify_rejects_a_non_uuid_user_id(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)

    # Act
    response = client.post(
        "/v1/ops/users/not-a-uuid/kyc/verify", json={"outcome": "verified"}, headers=_bearer(token)
    )

    # Assert
    assert response.status_code == 422


# --- GET /v1/ops/data-requests -----------------------------------------------------------------------------


def _insert_request(
    session_factory: sessionmaker[Session],
    user_id: uuid.UUID,
    status: str,
    received_at: datetime,
    due_at: datetime,
    completed_at: datetime | None,
) -> uuid.UUID:
    request_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO gateway.data_subject_requests "
                "(id, user_id, kind, status, received_at, due_at, completed_at, detail) "
                "VALUES (:id, :user_id, 'access', :status, :received_at, :due_at, :completed_at, '{}')"
            ),
            {
                "id": request_id,
                "user_id": user_id,
                "status": status,
                "received_at": received_at,
                "due_at": due_at,
                "completed_at": completed_at,
            },
        )
    return request_id


def test_data_request_list_filters_overdue_requests(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)
    user_id = _sign_up(session_factory, clock, "first@example.com")
    now = clock()
    overdue = _insert_request(
        session_factory, user_id, "received", now - timedelta(days=40), now - timedelta(days=10), None
    )
    pending = _insert_request(
        session_factory, user_id, "received", now - timedelta(days=1), now + timedelta(days=29), None
    )
    done = _insert_request(
        session_factory,
        user_id,
        "completed",
        now - timedelta(days=40),
        now - timedelta(days=10),
        now - timedelta(days=39),
    )

    # Act
    everything = client.get(DATA_REQUESTS, headers=_bearer(token))
    only_overdue = client.get(DATA_REQUESTS, params={"overdue": "true"}, headers=_bearer(token))
    not_overdue = client.get(DATA_REQUESTS, params={"overdue": "false"}, headers=_bearer(token))

    # Assert
    ids = {str(overdue), str(pending), str(done)}
    assert {item["request_id"] for item in everything.json()["items"]} == ids
    assert [item["request_id"] for item in only_overdue.json()["items"]] == [str(overdue)]
    not_overdue_ids = {item["request_id"] for item in not_overdue.json()["items"]}
    assert str(overdue) not in not_overdue_ids
    assert str(pending) in not_overdue_ids


def test_data_request_list_items_carry_the_spec_keys(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, "ops_lead")
    user_id = _sign_up(session_factory, clock, "first@example.com")
    now = clock()
    request_id = _insert_request(session_factory, user_id, "received", now, now + timedelta(days=30), None)

    # Act
    response = client.get(DATA_REQUESTS, headers=_bearer(token))

    # Assert
    items = response.json()["items"]
    assert response.status_code == 200
    assert len(items) == 1
    assert set(items[0]) == DSR_ITEM_KEYS
    assert (items[0]["request_id"], items[0]["user_id"]) == (str(request_id), str(user_id))
    assert (items[0]["kind"], items[0]["status"], items[0]["completed_at"]) == ("access", "received", None)
    assert datetime.fromisoformat(items[0]["due_at"]) == now + timedelta(days=30)


def test_data_request_list_rejects_a_non_boolean_overdue_filter(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock)

    # Act
    response = client.get(DATA_REQUESTS, params={"overdue": "maybe"}, headers=_bearer(token))

    # Assert
    assert response.status_code == 422


# --- GET /v1/ops/audit -------------------------------------------------------------------------------------


def test_audit_read_is_forbidden_for_ops_agent(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, "ops_agent")

    # Act
    response = client.get(AUDIT, headers=_bearer(token))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN


def test_audit_read_for_ops_lead_lists_the_login_row_with_the_spec_keys(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, enrolment = _ops_session(client, session_factory, clock, "ops_lead")

    # Act
    response = client.get(AUDIT, headers=_bearer(token))

    # Assert
    logins = [item for item in response.json()["items"] if item["action"] == "ops.login"]
    assert response.status_code == 200
    assert len(logins) == 1
    assert set(logins[0]) == AUDIT_ITEM_KEYS
    assert logins[0]["actor_id"] == f"ops:{enrolment.ops_user_id}"
    assert logins[0]["actor_role"] == "ops_lead"
    assert datetime.fromisoformat(logins[0]["occurred_at"]) == clock()


def test_audit_read_returns_the_newest_rows_first_and_filters_by_target(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, "ops_lead")
    for action in ("seed.one", "seed.two", "seed.three"):
        clock.advance(timedelta(seconds=1))
        _record(session_factory, clock, action, "seed:target")
    clock.advance(timedelta(seconds=1))
    _record(session_factory, clock, "seed.other", "seed:other")

    # Act
    response = client.get(AUDIT, params={"target": "seed:target"}, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert [item["action"] for item in response.json()["items"]] == ["seed.three", "seed.two", "seed.one"]
    assert {item["target"] for item in response.json()["items"]} == {"seed:target"}


def test_audit_read_limit_returns_only_the_newest_rows(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, "ops_lead")
    for action in ("seed.one", "seed.two", "seed.three"):
        clock.advance(timedelta(seconds=1))
        _record(session_factory, clock, action, "seed:target")

    # Act
    response = client.get(AUDIT, params={"target": "seed:target", "limit": 2}, headers=_bearer(token))

    # Assert
    assert response.status_code == 200
    assert [item["action"] for item in response.json()["items"]] == ["seed.three", "seed.two"]


@pytest.mark.parametrize("limit", [0, -1, 101, "abc"])
def test_audit_read_rejects_an_out_of_range_limit(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair, limit: object
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    token, _ = _ops_session(client, session_factory, clock, "ops_lead")

    # Act
    response = client.get(AUDIT, params={"limit": str(limit)}, headers=_bearer(token))

    # Assert
    assert response.status_code == 422


# --- merchant support keys ---------------------------------------------------------------------------------


def _merchant_with_support_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[uuid.UUID, str, str]:
    credentials = bootstrap_merchant(session_factory, clock, "Acme", TEST_API_KEY_PEPPER)
    with session_factory.begin() as session:
        support = issue_api_key(
            session, clock, credentials.merchant_id, TEST_API_KEY_PEPPER, role="merchant_support"
        )
    return credentials.merchant_id, credentials.api_key, support.api_key


def test_authenticate_api_key_role_returns_the_merchant_and_the_key_role(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    merchant_id, admin_key, support_key = _merchant_with_support_key(session_factory, clock)

    # Act
    with session_factory() as session:
        admin = authenticate_api_key_role(session, admin_key, TEST_API_KEY_PEPPER)
        support = authenticate_api_key_role(session, support_key, TEST_API_KEY_PEPPER)
        unknown = authenticate_api_key_role(session, "sk_test_unknown", TEST_API_KEY_PEPPER)

    # Assert
    assert admin == (merchant_id, "merchant_admin")
    assert support == (merchant_id, "merchant_support")
    assert unknown is None


def test_merchant_support_key_can_read_payments(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    _, _, support_key = _merchant_with_support_key(session_factory, clock)

    # Act
    response = client.get("/v1/payments", headers=_bearer(support_key))

    # Assert
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_merchant_support_key_cannot_create_a_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    _, _, support_key = _merchant_with_support_key(session_factory, clock)

    # Act
    response = client.post(
        "/v1/payments", json=CHARGE_BODY, headers={**_bearer(support_key), "Idempotency-Key": "support-1"}
    )

    # Assert
    payments = _rows(session_factory, "SELECT id FROM gateway.payments")
    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert payments == []


def test_merchant_support_key_cannot_refund_but_the_admin_key_reaches_the_refund_logic(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    _, admin_key, support_key = _merchant_with_support_key(session_factory, clock)
    path = f"/v1/payments/{uuid.uuid4()}/refunds"

    # Act
    support = client.post(path, json={}, headers={**_bearer(support_key), "Idempotency-Key": "r-1"})
    admin = client.post(path, json={}, headers={**_bearer(admin_key), "Idempotency-Key": "r-1"})

    # Assert
    assert (support.status_code, support.json()) == (403, FORBIDDEN)
    assert (admin.status_code, admin.json()) == (404, {"error": "payment_not_found"})


def test_merchant_support_key_can_read_a_dispute_and_submit_evidence(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    _, _, support_key = _merchant_with_support_key(session_factory, clock)
    dispute_id = uuid.uuid4()

    # Act
    read = client.get(f"/v1/disputes/{dispute_id}", headers=_bearer(support_key))
    evidence = client.post(
        f"/v1/disputes/{dispute_id}/evidence",
        json={"text": "proof of delivery"},
        headers=_bearer(support_key),
    )

    # Assert
    assert (read.status_code, read.json()) == (404, {"detail": "dispute not found"})
    assert (evidence.status_code, evidence.json()) == (404, {"detail": "dispute not found"})


def test_merchant_token_issued_for_a_support_key_carries_the_support_role_claim(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    merchant_id, _, support_key = _merchant_with_support_key(session_factory, clock)

    # Act
    response = client.post("/v1/merchant-tokens", headers=_bearer(support_key))

    # Assert
    assert response.status_code == 201
    claims = _ops_ai_claims(response.json()["access_token"], key_pair.public_pem)
    assert (claims["sub"], claims["merchant_id"], claims["roles"]) == (
        f"merchant:{merchant_id}",
        str(merchant_id),
        ["merchant_support"],
    )


def test_merchant_token_issued_for_an_admin_key_carries_the_admin_role_claim(
    session_factory: sessionmaker[Session], clock: FixedClock, key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, clock, key_pair.private_pem)
    merchant_id, admin_key, _ = _merchant_with_support_key(session_factory, clock)

    # Act
    response = client.post("/v1/merchant-tokens", headers=_bearer(admin_key))

    # Assert
    assert response.status_code == 201
    claims = _ops_ai_claims(response.json()["access_token"], key_pair.public_pem)
    assert (claims["sub"], claims["merchant_id"], claims["roles"]) == (
        f"merchant:{merchant_id}",
        str(merchant_id),
        ["merchant_admin"],
    )
