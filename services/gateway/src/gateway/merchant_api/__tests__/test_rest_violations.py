"""B8.3 on REST: the 422 body gains field-level `violations`; GET /v1/payments uses the shared validator."""

import uuid

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

NON_TEST_LUHN_PAN = "4111111111111111"


def _client_and_auth(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[TestClient, dict[str, str]]:
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
    client = TestClient(create_app(GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER)))
    return client, {"Authorization": f"Bearer {credentials.api_key}"}


def _body(**overrides: object) -> dict[str, object]:
    card: dict[str, object] = {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}
    body: dict[str, object] = {"amount_minor": 1500, "currency": "USD", "method": "card", "card": card}
    for key, value in overrides.items():
        if key.startswith("card_"):
            card[key.removeprefix("card_")] = value
        else:
            body[key] = value
    return body


def _post(client: TestClient, auth: dict[str, str], body: dict[str, object]) -> tuple[int, str, object]:
    response = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": uuid.uuid4().hex})
    return response.status_code, response.text, response.json()


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"amount_minor": 0}, "amount_minor"),
        ({"currency": "GBP"}, "currency"),
        ({"card_number": NON_TEST_LUHN_PAN}, "card.number"),
        ({"card_cvc": "12345"}, "card.cvc"),
    ],
)
def test_post_payments_422_lists_field_violations(
    session_factory: sessionmaker[Session], clock: FixedClock, overrides: dict[str, object], field: str
) -> None:
    # Arrange
    client, auth = _client_and_auth(session_factory, clock)

    # Act
    status, _text, payload = _post(client, auth, _body(**overrides))

    # Assert
    assert status == 422
    assert isinstance(payload, dict)
    assert payload["error"] == "validation_failed"
    violations = payload["violations"]
    assert isinstance(violations, list)
    assert [v["field"] for v in violations] == [field]
    assert all(isinstance(v["description"], str) and v["description"] for v in violations)


def test_post_payments_422_keeps_the_existing_detail_list(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client, auth = _client_and_auth(session_factory, clock)

    # Act
    status, _text, payload = _post(client, auth, _body(amount_minor=0))

    # Assert
    assert status == 422
    assert isinstance(payload, dict)
    detail = payload["detail"]
    assert isinstance(detail, list)
    assert [(tuple(d["loc"]), d["type"]) for d in detail] == [(("body", "amount_minor"), "greater_than")]
    assert payload["violations"] == [{"field": "amount_minor", "description": detail[0]["msg"]}]


@pytest.mark.parametrize("pan", [NON_TEST_LUHN_PAN, "4242424242424241", "4" * 40])
def test_post_payments_422_never_echoes_the_pan(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str
) -> None:
    # Arrange
    client, auth = _client_and_auth(session_factory, clock)

    # Act
    status, text, _payload = _post(client, auth, _body(card_number=pan))

    # Assert
    assert status == 422
    assert pan not in text


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("created_from=2026-11-01T00:00:00", "invalid created_from"),
        ("created_to=nope", "invalid created_to"),
        ("created_from=2026-11-02T00:00:00Z&created_to=2026-11-01T00:00:00Z", "invalid date range"),
        ("method=wallet", "invalid method"),
        ("cursor=not-a-cursor", "invalid cursor"),
        ("created_from=bad&method=wallet", "invalid created_from"),
    ],
)
def test_get_payments_keeps_the_first_violation_as_400_detail(
    session_factory: sessionmaker[Session], clock: FixedClock, query: str, detail: str
) -> None:
    # Arrange
    client, auth = _client_and_auth(session_factory, clock)

    # Act
    response = client.get(f"/v1/payments?{query}", headers=auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": detail}
