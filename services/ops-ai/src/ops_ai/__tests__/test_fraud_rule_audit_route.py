"""Rule audit HTTP contract from r2-agents.md section 6.1."""

import uuid
from dataclasses import dataclass

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.clock import Clock
from ops_ai.feedback.routes import FraudRulesOut, RuleVersionOut
from ops_ai.scope import MerchantRole
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

AUDIT_ROUTE = "/v1/feedback/fraud-rules/audit"
MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c01")
OTHER_MERCHANT = uuid.UUID("0b1c6a52-3f1e-4c55-9d3a-6f7e2a1b9c02")
USER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
VERSION_FIELDS = {
    "version",
    "steps",
    "min_micro_payments",
    "min_micro_share",
    "min_distinct_cards",
    "reason",
    "feedback_id",
    "created_at",
}


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
        raise AssertionError(f"audit must not narrate: {request}")


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


def _token(
    key_pair: KeyPair,
    merchant_id: uuid.UUID = MERCHANT,
    role: MerchantRole = "merchant_admin",
    *,
    kind: str = "merchant",
    expired: bool = False,
) -> str:
    subject_id = merchant_id if kind == "merchant" else USER
    issued = int(TEST_NOW.timestamp()) - (901 if expired else 0)
    return jwt.encode(
        {
            "iss": "gateway",
            "aud": "ops-ai",
            "sub": f"{kind}:{subject_id}",
            f"{'ops_user' if kind == 'ops' else kind}_id": str(subject_id),
            "roles": [role if kind == "merchant" else "ops_agent" if kind == "ops" else "consumer"],
            "iat": issued,
            "exp": issued + 900,
            "jti": str(uuid.uuid4()),
        },
        key_pair.private_pem,
        algorithm="RS256",
    )


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _insert_version(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    version: int,
    role: MerchantRole | None = None,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.fraud_rule_versions "
                "(merchant_id, version, steps, min_micro_payments, min_micro_share, min_distinct_cards, "
                "reason, feedback_id, created_at) VALUES (:merchant, :version, 1, 12, 0.83, 4, "
                "'revert:v0', NULL, :now)"
            ),
            {"merchant": merchant_id, "version": version, "now": TEST_NOW},
        )
        if role is not None:
            session.execute(
                text(
                    "INSERT INTO ops_ai.fraud_rule_version_actors "
                    "(merchant_id, version, actor, actor_role, created_at) "
                    "VALUES (:merchant, :version, :actor, :role, :now)"
                ),
                {
                    "merchant": merchant_id,
                    "version": version,
                    "actor": f"merchant:{merchant_id}",
                    "role": role,
                    "now": TEST_NOW,
                },
            )


def _entry(version: int, role: MerchantRole | None) -> dict[str, object]:
    return {
        "version": version,
        "reason": "revert:v0",
        "feedback_id": None,
        "actor": None if role is None else f"merchant:{MERCHANT}",
        "actor_role": role,
        "created_at": TEST_NOW.isoformat().replace("+00:00", "Z"),
    }


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
def test_audit_empty_merchant_returns_empty_entries_and_no_store(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    clock: Clock,
    role: MerchantRole,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_version(session_factory, OTHER_MERCHANT, 1, "merchant_admin")

    # Act
    response = client.get(AUDIT_ROUTE, headers=_headers(_token(key_pair, role=role)))

    # Assert
    assert response.status_code == 200
    assert response.json() == {"entries": []}
    assert response.headers["cache-control"] == "no-store"


def test_audit_returns_exact_entries_newest_first_with_legacy_null_actor_and_tenant_isolation(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    clock: Clock,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    _insert_version(session_factory, MERCHANT, 1)
    _insert_version(session_factory, MERCHANT, 3, "merchant_support")
    _insert_version(session_factory, MERCHANT, 2, "merchant_admin")
    _insert_version(session_factory, OTHER_MERCHANT, 4, "merchant_support")

    # Act
    response = client.get(
        AUDIT_ROUTE,
        headers=_headers(_token(key_pair)),
        params={"merchant_id": str(OTHER_MERCHANT), "actor_role": "merchant_support"},
    )

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "entries": [
            _entry(3, "merchant_support"),
            _entry(2, "merchant_admin"),
            _entry(1, None),
        ]
    }
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("credential", ["missing", "malformed", "expired", "wrong_signature"])
def test_audit_rejects_missing_or_invalid_credentials_with_401(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    clock: Clock,
    credential: str,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    token = _token(key_pair, expired=credential == "expired")
    if credential == "wrong_signature":
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode()
        token = _token(KeyPair(private, key_pair.public_pem))
    headers = {} if credential == "missing" else _headers("invalid" if credential == "malformed" else token)

    # Act
    response = client.get(AUDIT_ROUTE, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid token"}


@pytest.mark.parametrize("kind", ["user", "ops"])
def test_audit_rejects_other_principal_kinds_with_403(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    clock: Clock,
    kind: str,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)

    # Act
    response = client.get(AUDIT_ROUTE, headers=_headers(_token(key_pair, kind=kind)))

    # Assert
    assert response.status_code == 403
    assert response.json() == {"detail": "forbidden"}


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
def test_feedback_and_revert_audit_the_token_principal_and_preserve_existing_response_shapes(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    clock: Clock,
    role: MerchantRole,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    headers = _headers(_token(key_pair, role=role))
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, "
                "started_at, last_seen_at, payment_count, features, created_at) VALUES "
                "(:id, :window, :merchant, 'card_testing', :payment, :now, :now, 12, '{}'::jsonb, :now)"
            ),
            {
                "id": alert_id,
                "window": f"card_testing:{MERCHANT}:{alert_id}",
                "merchant": MERCHANT,
                "payment": uuid.uuid4(),
                "now": TEST_NOW,
            },
        )

    # Act
    feedback = client.post(
        "/v1/feedback",
        headers=headers,
        json={
            "target_kind": "alert",
            "target_id": str(alert_id),
            "verdict": "false_positive",
        },
    )
    reverted = client.post("/v1/feedback/fraud-rules/revert", headers=headers, json={"version": 0})
    audit = client.get(AUDIT_ROUTE, headers=headers)
    rules = client.get("/v1/feedback/fraud-rules", headers=headers)

    # Assert
    assert feedback.status_code == 201
    assert reverted.status_code == 201
    assert audit.status_code == 200
    assert rules.status_code == 200
    assert audit.json() == {
        "entries": [
            _entry(2, role),
            {
                **_entry(1, role),
                "reason": "feedback:false_positive",
                "feedback_id": feedback.json()["feedback_id"],
            },
        ]
    }
    assert set(feedback.json()) == {
        "feedback_id",
        "target_kind",
        "target_id",
        "verdict",
        "rule_version",
        "incident_score",
    }
    assert set(feedback.json()["rule_version"]) == VERSION_FIELDS
    assert set(reverted.json()) == VERSION_FIELDS
    assert set(rules.json()) == {"active", "history"}
    assert rules.json()["active"] == reverted.json()
    assert rules.json()["history"] == [reverted.json(), feedback.json()["rule_version"]]
    assert set(RuleVersionOut.model_fields) == VERSION_FIELDS
    assert set(FraudRulesOut.model_fields) == {"active", "history"}
    assert audit.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("path", ["/v1/feedback", "/v1/feedback/fraud-rules/revert"])
def test_body_cannot_override_the_acting_merchant_or_role(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    clock: Clock,
    path: str,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair, clock)
    body: dict[str, object] = (
        {"version": 0}
        if path.endswith("revert")
        else {
            "target_kind": "answer",
            "target_id": "opaque",
            "verdict": "helpful",
        }
    )
    body.update({"merchant_id": str(OTHER_MERCHANT), "actor": "forged", "actor_role": "merchant_admin"})
    headers = _headers(_token(key_pair, role="merchant_support"))

    # Act
    response = client.post(path, headers=headers, json=body)
    audit = client.get(AUDIT_ROUTE, headers=headers)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert audit.status_code == 200
    assert audit.json() == {"entries": []}
