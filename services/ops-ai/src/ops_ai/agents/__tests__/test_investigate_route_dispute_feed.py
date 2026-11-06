"""The default deployment (ops_ai.main) judges dispute workflows from gw.disputes.v1 (R2-1 / R2-2)."""

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from contracts.events import DisputeEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.ingest.disputes import apply_dispute_event
from ops_ai.main import build_app_from_env
from sqlalchemy.orm import Session, sessionmaker

INVESTIGATE = "/v1/agents/investigate"
QUESTION = "Any dispute open?"
OPENED_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def pems() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


def _token(private_pem: str, merchant_id: uuid.UUID) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": "gateway",
        "aud": "ops-ai",
        "sub": f"merchant:{merchant_id}",
        "merchant_id": str(merchant_id),
        "roles": ["merchant_admin"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=15)).timestamp()),
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def _project_lost_dispute(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> uuid.UUID:
    dispute_id = uuid.uuid4()
    shared = {
        "dispute_id": dispute_id,
        "payment_id": uuid.uuid4(),
        "merchant_id": merchant_id,
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 1500,
        "refunded_minor": 0,
        "dispute_amount_minor": 1500,
    }
    steps = [
        ("dispute.opened", None, "opened", "card_network", 0, 0),
        ("dispute.evidence_submitted", "opened", "under_review", "merchant", 0, 0),
        ("dispute.resolved", "under_review", "lost", "ops_lead", 1500, 1500),
    ]
    for index, (event_type, from_state, to_state, actor_role, chargeback, fee) in enumerate(steps):
        event = DisputeEventV1.model_validate(
            {
                **shared,
                "event_id": uuid.uuid4(),
                "event_type": event_type,
                "occurred_at": OPENED_AT + timedelta(hours=index),
                "entity_version": index + 1,
                "from_state": from_state,
                "to_state": to_state,
                "actor_role": actor_role,
                "chargeback_minor": chargeback,
                "chargeback_fee_minor": fee,
            }
        )
        with session_factory.begin() as session:
            apply_dispute_event(session, lambda: OPENED_AT, event)
    return dispute_id


def _client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, public_pem: str) -> TestClient:
    for name in ("OPS_AI_JWT_PUBLIC_KEY_PEM", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "ops_ai_jwt_public_key_pem").write_text(public_pem)
    monkeypatch.setenv("OPS_AI_SECRETS_DIR", str(tmp_path))
    return TestClient(build_app_from_env())


def _dispute_finding(body: dict[str, object]) -> dict[str, object]:
    messages = body["messages"]
    assert isinstance(messages, list)
    findings = [
        message["payload"]
        for message in messages
        if message["sender"] == "dispute"
        and message["recipient"] == "supervisor"
        and message["intent"] == "finding"
    ]
    assert len(findings) == 1
    payload = findings[0]
    assert isinstance(payload, dict)
    return payload


@pytest.mark.usefixtures("redis_client")
def test_the_default_deployment_judges_the_merchants_projected_dispute_workflow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    private_pem, public_pem = pems
    merchant_id = uuid.uuid4()
    _project_lost_dispute(session_factory, merchant_id)
    client = _client(monkeypatch, tmp_path, public_pem)
    headers = {"Authorization": f"Bearer {_token(private_pem, merchant_id)}"}

    # Act
    response = client.post(INVESTIGATE, json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 200
    body = response.json()
    finding = _dispute_finding(body)
    assert (finding["judge_status"], finding["judge_reason"], finding["judge_disputes"]) == (
        "judged",
        None,
        1,
    )
    assert (finding["judge_verdict"], finding["judge_passed"], finding["judge_score"]) == ("valid", True, 1.0)
    assert f"dispute_judge:{merchant_id}" in [item["ref"] for item in body["evidence"]]


@pytest.mark.usefixtures("redis_client")
def test_the_default_deployment_never_judges_another_merchants_dispute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    pems: tuple[str, str],
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    private_pem, public_pem = pems
    merchant_id = uuid.uuid4()
    _project_lost_dispute(session_factory, uuid.uuid4())
    client = _client(monkeypatch, tmp_path, public_pem)
    headers = {"Authorization": f"Bearer {_token(private_pem, merchant_id)}"}

    # Act
    response = client.post(INVESTIGATE, json={"question": QUESTION}, headers=headers)

    # Assert
    assert response.status_code == 200
    finding = _dispute_finding(response.json())
    assert (finding["judge_status"], finding["judge_reason"]) == (
        "skipped",
        "no dispute workflow record for this merchant",
    )
