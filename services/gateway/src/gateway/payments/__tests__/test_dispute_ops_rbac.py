"""RBAC tests for POST /v1/ops/disputes/{dispute_id}/resolution with an ops session, written from
docs/specs/compliance-rbac.md section 1 (`dispute.resolve` is allowed to `ops_lead` only).

Any Authorization header (whatever its scheme) must carry a valid ops session allowed `dispute.resolve`; the
interim `X-Ops-Admin-Token` is only consulted when the request has no Authorization header at all, so it can
never rescue a bad or missing session credential.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.rbac import OpsRole
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.admin import OPS_SESSION_TTL_SECONDS, OpsPrincipal, bootstrap_ops_user, issue_ops_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
PASSWORD = "dispute-ops-password-7Q"
NETWORK_TOKEN = "test-network-token-not-a-secret"
OPS_TOKEN = "test-ops-admin-token-not-a-secret"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
OPS_ADMIN_HEADER = {"X-Ops-Admin-Token": OPS_TOKEN}
INVALID_OPS_SESSION = {"detail": "invalid ops session"}
FORBIDDEN = {"detail": "forbidden"}
UNDER_REVIEW = "under_review"


@dataclass(frozen=True)
class Env:
    client: TestClient
    private_pem: str
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_auth: dict[str, str]
    dispute_id: uuid.UUID


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


# --- arrange helpers -------------------------------------------------------------------------------------


def _env(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    ops_admin_token: str | None = OPS_TOKEN,
) -> Env:
    """An app with one `under_review` dispute (network-opened, evidence submitted)."""
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    client = TestClient(
        create_app(
            GatewayAppDeps(
                charge=deps,
                api_key_pepper=TEST_API_KEY_PEPPER,
                jwt_private_key_pem=private_pem,
                network_token=NETWORK_TOKEN,
                ops_admin_token=ops_admin_token,
            )
        )
    )
    merchant_auth = {"Authorization": f"Bearer {credentials.api_key}"}
    request = ChargeRequest.model_validate(
        {
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "card": {"number": APPROVE_PAN, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )
    outcome = charge_card(deps, credentials.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    opened = client.post(
        "/v1/network/disputes",
        json={"payment_id": str(outcome.body["payment_id"]), "reason": "fraudulent"},
        headers={"X-Network-Token": NETWORK_TOKEN},
    )
    assert opened.status_code == 201
    dispute_id = uuid.UUID(str(opened.json()["dispute_id"]))
    evidence = client.post(
        f"/v1/disputes/{dispute_id}/evidence", json={"text": "receipt attached"}, headers=merchant_auth
    )
    assert evidence.status_code == 200
    return Env(client, private_pem, session_factory, clock, merchant_auth, dispute_id)


def _ops_session(env: Env, role: OpsRole, email: str = "ops@example.com") -> tuple[uuid.UUID, str]:
    enrolment = bootstrap_ops_user(env.session_factory, env.clock, email, PASSWORD, role, TOTP_KEY)
    principal = OpsPrincipal(ops_user_id=enrolment.ops_user_id, role=role)
    return enrolment.ops_user_id, issue_ops_session_token(env.private_pem, principal, env.clock())


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _resolve(env: Env, headers: dict[str, str], outcome: str = "lost") -> Response:
    response: Response = env.client.post(
        f"/v1/ops/disputes/{env.dispute_id}/resolution", json={"outcome": outcome}, headers=headers
    )
    return response


# --- assert helpers --------------------------------------------------------------------------------------


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _dispute_state(env: Env) -> str:
    [row] = _rows(env.session_factory, "SELECT state FROM gateway.disputes WHERE id = :id", id=env.dispute_id)
    return str(row["state"])


def _resolution_audit(env: Env) -> list[dict[str, object]]:
    return _rows(
        env.session_factory,
        "SELECT actor_id, actor_role, action, target FROM gateway.audit_log "
        "WHERE action = 'dispute.resolved' ORDER BY id",
    )


# --- ops_lead resolves; ops_agent is forbidden -----------------------------------------------------------


def test_an_ops_lead_session_resolves_an_under_review_dispute_with_200(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _ops_user_id, token = _ops_session(env, "ops_lead")

    # Act
    response = _resolve(env, _bearer(token), "lost")

    # Assert
    assert response.status_code == 200
    assert response.json()["dispute_id"] == str(env.dispute_id)
    assert response.json()["state"] == "lost"
    assert _dispute_state(env) == "lost"


def test_an_ops_lead_resolution_is_audited_as_the_ops_user_and_role(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    ops_user_id, token = _ops_session(env, "ops_lead")

    # Act
    _resolve(env, _bearer(token), "won")

    # Assert
    assert _resolution_audit(env) == [
        {
            "actor_id": f"ops:{ops_user_id}",
            "actor_role": "ops_lead",
            "action": "dispute.resolved",
            "target": f"dispute:{env.dispute_id}",
        }
    ]


def test_an_ops_agent_session_is_forbidden_with_403_and_the_dispute_stays_under_review(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _ops_user_id, token = _ops_session(env, "ops_agent")

    # Act
    response = _resolve(env, _bearer(token))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _dispute_state(env) == UNDER_REVIEW
    assert _resolution_audit(env) == []


# --- credentials that are not a valid ops session --------------------------------------------------------


def test_a_consumer_session_is_rejected_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    with session_factory() as session:
        user_id = sign_up(session, clock, "consumer@example.com", PASSWORD).user_id
        session.commit()
    token = issue_session_token(private_pem, user_id, clock(), ("pwd",))

    # Act
    response = _resolve(env, _bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW


def test_a_merchant_api_key_is_rejected_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _resolve(env, env.merchant_auth)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW


def test_an_expired_ops_session_is_rejected_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _ops_user_id, token = _ops_session(env, "ops_lead")
    clock.advance(timedelta(seconds=OPS_SESSION_TTL_SECONDS + 1))

    # Act
    response = _resolve(env, _bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW


def test_a_malformed_ops_token_is_rejected_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _resolve(env, _bearer("not-a-jwt"))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW


# --- the interim token never rescues a bad Authorization header ------------------------------------------


@pytest.mark.parametrize(
    "authorization",
    ["Bearer garbage", "Basic abc", "Bearer "],
    ids=["bearer-garbage", "basic-scheme", "empty-bearer"],
)
def test_an_authorization_header_with_the_correct_interim_token_alongside_is_still_a_session_failure(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, authorization: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    headers = {"Authorization": authorization, **OPS_ADMIN_HEADER}

    # Act
    response = _resolve(env, headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW
    assert _resolution_audit(env) == []


def test_an_ops_agent_session_with_the_correct_interim_token_alongside_is_still_forbidden(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _ops_user_id, token = _ops_session(env, "ops_agent")

    # Act
    response = _resolve(env, {**_bearer(token), **OPS_ADMIN_HEADER})

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN
    assert _dispute_state(env) == UNDER_REVIEW


# --- no interim token configured -------------------------------------------------------------------------


def test_the_ops_session_path_works_when_no_interim_token_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, ops_admin_token=None)
    ops_user_id, token = _ops_session(env, "ops_lead")

    # Act
    response = _resolve(env, _bearer(token), "won")

    # Assert
    assert response.status_code == 200
    assert response.json()["state"] == "won"
    assert [row["actor_id"] for row in _resolution_audit(env)] == [f"ops:{ops_user_id}"]


# --- the stored ops user is re-checked on every request --------------------------------------------------


def test_a_disabled_ops_user_is_rejected_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    ops_user_id, token = _ops_session(env, "ops_lead")
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.ops_users SET disabled_at = :now WHERE id = :id"),
            {"now": clock(), "id": ops_user_id},
        )

    # Act
    response = _resolve(env, _bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW


def test_an_ops_user_whose_stored_role_changed_is_rejected_with_401_invalid_ops_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    ops_user_id, token = _ops_session(env, "ops_lead")
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE gateway.ops_users SET role = 'ops_agent' WHERE id = :id"), {"id": ops_user_id}
        )

    # Act
    response = _resolve(env, _bearer(token))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_OPS_SESSION
    assert _dispute_state(env) == UNDER_REVIEW
