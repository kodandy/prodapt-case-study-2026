"""POST /v1/assistant/investigate searches incidents with deps.embedder (docs/specs/sq1.md section 10.4)."""

import uuid
from datetime import timedelta
from typing import Literal

import jwt
from contracts.events import PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.investigation.models import IncidentRecord
from ops_ai.search.__tests__.fake_embedders import RecordingEmbedder
from ops_ai.search.embedder import LocalHashEmbedder
from ops_ai.search.indexing import index_incidents
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

QUESTION = "15% of my card payments are failing this afternoon - what is the root cause and how do we fix it?"
ROUTE = "/v1/assistant/investigate"
WINDOW_START = TEST_NOW - timedelta(hours=6)
BASELINE_START = WINDOW_START - timedelta(days=7)


def _key_pair() -> tuple[str, str]:
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


def _event(
    merchant_id: uuid.UUID, minutes_after: int, *, declined: bool, start: Literal["window", "baseline"]
) -> PaymentEventV1:
    occurred_at = (WINDOW_START if start == "window" else BASELINE_START) + timedelta(minutes=minutes_after)
    return PaymentEventV1(
        event_id=uuid.uuid4(),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=occurred_at,
        entity_version=1,
        payment_id=uuid.uuid4(),
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="issuer_unavailable" if declined else None,
        card_bin="400000" if declined else "424242",
        card_last4="0119" if declined else "4242",
    )


def _seed(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> None:
    events = [
        *(_event(merchant_id, 10 * (index + 1), declined=True, start="window") for index in range(10)),
        *(_event(merchant_id, 5 + index, declined=False, start="window") for index in range(10)),
        *(_event(merchant_id, index + 1, declined=False, start="baseline") for index in range(20)),
    ]
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
        session.add(
            IncidentRecord(
                incident_id="inc_a",
                merchant_id=merchant_id,
                title="Issuer outage on BIN 400000",
                root_cause="Issuer timeouts on BIN 400000",
                fix_applied="Rerouted BIN 400000 to acq_b",
                resolution_notes="notes",
                started_at=TEST_NOW - timedelta(days=50),
                resolved_at=TEST_NOW - timedelta(days=50) + timedelta(hours=3),
                recurrence_within_7d=False,
            )
        )
        session.flush()
        index_incidents(session)


def test_investigate_route_searches_incidents_with_the_app_embedder(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    _seed(session_factory, merchant_id)
    private_pem, public_pem = _key_pair()
    embedder = RecordingEmbedder(LocalHashEmbedder())
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=MockLlm(),
        jwt_public_key_pem=public_pem,
        clock=lambda: TEST_NOW,
        embedder=embedder,
    )
    client = TestClient(create_app(deps))

    # Act
    response = client.post(
        ROUTE,
        json={"question": QUESTION},
        headers={"Authorization": f"Bearer {_token(private_pem, merchant_id)}"},
    )

    # Assert
    assert response.status_code == 200
    body = response.json()
    assert embedder.calls == [["BIN 400000 issuer"]]
    assert "incident:inc_a" in [item["ref"] for item in body["evidence"]]
    assert body["tool_calls"][2] == {"name": "search_incidents", "args": {"query": "BIN 400000 issuer"}}
