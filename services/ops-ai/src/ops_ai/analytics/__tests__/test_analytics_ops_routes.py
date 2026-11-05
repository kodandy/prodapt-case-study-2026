"""Ops-principal contract tests from docs/specs/analytics.md section 4."""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
import pytest
from contracts.events import EventOrigin, PaymentEventV1
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from httpx import Response
from ops_ai.analytics.access import ForbiddenOpsError, OpsAccess, require_ops_principal
from ops_ai.analytics.chargeback import PRIOR_RATE
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import OPS_ROLES, OpsRole, OpsScope, Principal, Scope, UserScope
from ops_ai.settlement.models import SettlementEntry
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
MERCHANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b2")
UNKNOWN_MERCHANT = uuid.UUID("dddddddd-0000-4000-8000-0000000000d4")
USER = uuid.UUID("cccccccc-0000-4000-8000-0000000000c3")
OPS_USER = uuid.UUID("eeeeeeee-0000-4000-8000-0000000000e5")
FORECAST = "/v1/analytics/forecast"
RISK = "/v1/analytics/chargeback-risk"
ALERTS = "/v1/analytics/alerts"
OPS_FORECAST = "/v1/ops/analytics/forecast"
OPS_RISK = "/v1/ops/analytics/chargeback-risk"
OPS_ALERTS = "/v1/ops/analytics/alerts"
MERCHANT_ROUTES = (FORECAST, RISK, ALERTS)
OPS_ROUTES = (OPS_FORECAST, OPS_RISK, OPS_ALERTS)
FORBIDDEN_BODY = {"detail": "forbidden"}
INVALID_TOKEN_BODY = {"detail": "invalid token"}
RISK_KEYS = {"captured_count", "chargeback_count", "merchant_chargeback_rate", "model", "items"}
MODEL_KEYS = {
    "features",
    "train_count",
    "test_count",
    "test_positives",
    "auc",
    "brier",
    "base_rate_brier",
    "trained_on",
}
OPS_ALERT_KEYS = {
    "alert_id",
    "merchant_id",
    "metric",
    "detail",
    "bucket_start",
    "observed",
    "expected",
    "zscore",
    "attempts",
    "created_at",
    "updated_at",
}
CLOCK_A = TEST_NOW + timedelta(minutes=10)
CLOCK_B = TEST_NOW + timedelta(minutes=12)


@dataclass
class KeyPair:
    private_pem: str
    public_pem: str


@pytest.fixture(scope="module")
def key_pair() -> KeyPair:
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


class _UnusedLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise AssertionError("the analytics routes never call the LLM")


def _client(session_factory: sessionmaker[Session], key_pair: KeyPair) -> TestClient:
    deps = OpsAiAppDeps(
        session_factory=session_factory,
        llm=_UnusedLlm(),
        jwt_public_key_pem=key_pair.public_pem,
        clock=lambda: TEST_NOW,
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


def _ops_token(key_pair: KeyPair, role: OpsRole, *, expired: bool = False) -> str:
    claims = _claims(f"ops:{OPS_USER}", {"ops_user_id": str(OPS_USER)}, [role])
    if expired:
        claims.update(iat=int(TEST_NOW.timestamp()) - 900, exp=int(TEST_NOW.timestamp()))
    return jwt.encode(claims, key_pair.private_pem, algorithm="RS256")


def _merchant_token(key_pair: KeyPair, merchant_id: uuid.UUID = MERCHANT_A) -> str:
    claims = _claims(f"merchant:{merchant_id}", {"merchant_id": str(merchant_id)}, ["merchant_admin"])
    return jwt.encode(claims, key_pair.private_pem, algorithm="RS256")


def _user_token(key_pair: KeyPair) -> str:
    claims = _claims(f"user:{USER}", {"user_id": str(USER)}, ["consumer"])
    return jwt.encode(claims, key_pair.private_pem, algorithm="RS256")


def _get(
    client: TestClient, path: str, token: str | None = None, params: Mapping[str, str] | None = None
) -> Response:
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    response: Response = client.get(path, headers=headers, params=dict(params or {}))
    return response


def _as_obj(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _objs(value: object) -> list[dict[str, object]]:
    assert isinstance(value, list)
    return [_as_obj(item) for item in value]


def _body(response: Response) -> dict[str, object]:
    return _as_obj(response.json())


def _at(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value)


def _ops_params(path: str, merchant_id: uuid.UUID = MERCHANT_A) -> dict[str, str]:
    return {} if path == OPS_ALERTS else {"merchant_id": str(merchant_id)}


def _payment(n: int, merchant_id: uuid.UUID, at: datetime, amount_minor: int) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10**9 + n),
        event_type="payment.succeeded",
        occurred_at=at,
        origin="backfill",
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency="USD",
        method="card",
        state="succeeded",
        provider="acq_a",
        decline_code=None,
        card_bin="424242",
        card_last4="4242",
    )


def _entry(n: int, merchant_id: uuid.UUID, *, chargeback: bool) -> SettlementEntry:
    return SettlementEntry(
        payment_id=uuid.UUID(int=50_000 + n),
        merchant_id=merchant_id,
        amount_minor=2500,
        currency="USD",
        captured_at=TEST_NOW - timedelta(days=60),
        statement_period="2026-09",
        refunded=False,
        chargeback=chargeback,
        ingest_source="seed",
    )


@pytest.fixture
def payments_seeded(session_factory: sessionmaker[Session]) -> None:
    day = timedelta(days=1)
    events = [
        _payment(1, MERCHANT_A, TEST_NOW - 2 * day, 2500),
        _payment(2, MERCHANT_A, TEST_NOW - day, 2500),
        _payment(3, MERCHANT_A, TEST_NOW - day - timedelta(hours=2), 5000),
        _payment(4, MERCHANT_B, TEST_NOW - day, 999_999),
    ]
    entries = [_entry(i, MERCHANT_A, chargeback=i < 2) for i in range(20)]
    entries.extend(_entry(30 + i, MERCHANT_B, chargeback=True) for i in range(5))
    with session_factory.begin() as session:
        for event in events:
            apply_payment_event(session, lambda: TEST_NOW, event)
        session.add_all(entries)


def _anomaly_event(
    n: int, merchant_id: uuid.UUID, at: datetime, *, declined: bool, origin: EventOrigin
) -> PaymentEventV1:
    return PaymentEventV1(
        event_id=uuid.UUID(int=10**9 + n),
        event_type="payment.declined" if declined else "payment.succeeded",
        occurred_at=at,
        origin=origin,
        entity_version=1,
        payment_id=uuid.UUID(int=n),
        merchant_id=merchant_id,
        amount_minor=1500,
        currency="USD",
        method="card",
        state="declined" if declined else "succeeded",
        provider="acq_a",
        decline_code="issuer_unavailable" if declined else None,
        card_bin="424242",
        card_last4="4242",
    )


def _seed_merchant_alerts(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, base: int, clock_at: datetime
) -> None:
    first_bucket = TEST_NOW - timedelta(minutes=15 * 8)
    history = [
        _anomaly_event(
            base + bucket * 12 + minute,
            merchant_id,
            first_bucket + timedelta(minutes=15 * bucket + minute),
            declined=minute == 0,
            origin="backfill",
        )
        for bucket in range(8)
        for minute in range(12)
    ]
    burst = [
        _anomaly_event(
            base + 1_000 + k, merchant_id, TEST_NOW + timedelta(minutes=k + 1), declined=True, origin="live"
        )
        for k in range(10)
    ]
    with session_factory.begin() as session:
        for event in history:
            apply_payment_event(session, lambda: clock_at, event)
        for event in burst:
            apply_payment_event(session, lambda: clock_at, event)


@pytest.fixture
def alerts_seeded(session_factory: sessionmaker[Session]) -> None:
    _seed_merchant_alerts(session_factory, MERCHANT_A, 1_000, CLOCK_A)
    _seed_merchant_alerts(session_factory, MERCHANT_B, 20_000, CLOCK_B)


def _stored_alert_ids(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> set[str]:
    with session_factory() as session:
        rows = session.execute(
            text("SELECT alert_id FROM ops_ai.anomaly_alerts WHERE merchant_id = :merchant_id"),
            {"merchant_id": merchant_id},
        ).scalars()
        return {str(alert_id) for alert_id in rows}


@pytest.mark.parametrize("role", OPS_ROLES)
@pytest.mark.parametrize("path", OPS_ROUTES)
def test_ops_roles_get_200_and_no_store_on_every_ops_analytics_route(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: OpsRole, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    token = _ops_token(key_pair, role)

    # Act
    response = _get(client, path, token=token, params=_ops_params(path))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("role", OPS_ROLES)
def test_ops_alerts_list_both_merchants_with_each_items_merchant_id(
    session_factory: sessionmaker[Session], key_pair: KeyPair, alerts_seeded: None, role: OpsRole
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    expected_ids = {
        str(merchant): _stored_alert_ids(session_factory, merchant) for merchant in (MERCHANT_A, MERCHANT_B)
    }

    # Act
    response = _get(client, OPS_ALERTS, token=_ops_token(key_pair, role))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = _body(response)
    assert set(body) == {"server_time", "items"}
    assert _at(body["server_time"]) == TEST_NOW
    items = _objs(body["items"])
    assert len(items) == 4
    assert [set(item) for item in items] == [OPS_ALERT_KEYS] * 4
    assert [item["merchant_id"] for item in items] == [str(MERCHANT_B)] * 2 + [str(MERCHANT_A)] * 2
    for merchant_id, alert_ids in expected_ids.items():
        merchant_items = [item for item in items if item["merchant_id"] == merchant_id]
        assert len(alert_ids) == 2
        assert {str(item["alert_id"]) for item in merchant_items} == alert_ids
        assert sorted(str(item["metric"]) for item in merchant_items) == ["decline_mix", "failure_rate"]


@pytest.mark.parametrize("role", OPS_ROLES)
@pytest.mark.parametrize("merchant_id", (MERCHANT_A, MERCHANT_B), ids=("merchant_a", "merchant_b"))
def test_ops_forecast_equals_the_selected_merchants_forecast_body(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    payments_seeded: None,
    role: OpsRole,
    merchant_id: uuid.UUID,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    params = {"horizon_days": "3"}
    expected_history = [
        {"day": "2026-11-01", "amount_minor": 2500 if merchant_id == MERCHANT_A else 0},
        {"day": "2026-11-02", "amount_minor": 7500 if merchant_id == MERCHANT_A else 999_999},
    ]

    # Act
    merchant_response = _get(client, FORECAST, token=_merchant_token(key_pair, merchant_id), params=params)
    ops_response = _get(
        client,
        OPS_FORECAST,
        token=_ops_token(key_pair, role),
        params={**params, "merchant_id": str(merchant_id)},
    )

    # Assert
    assert (merchant_response.status_code, ops_response.status_code) == (200, 200)
    assert ops_response.headers["Cache-Control"] == "no-store"
    assert _body(ops_response) == _body(merchant_response)
    body = _body(ops_response)
    assert body["horizon_days"] == 3
    [usd] = _objs(body["currencies"])
    assert usd["currency"] == "USD"
    assert _objs(usd["history"])[-2:] == expected_history
    assert [point["day"] for point in _objs(usd["forecast"])] == [
        "2026-11-03",
        "2026-11-04",
        "2026-11-05",
    ]


@pytest.mark.parametrize("role", OPS_ROLES)
@pytest.mark.parametrize("merchant_id", (MERCHANT_A, MERCHANT_B), ids=("merchant_a", "merchant_b"))
def test_ops_chargeback_risk_equals_the_selected_merchants_risk_body(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    payments_seeded: None,
    role: OpsRole,
    merchant_id: uuid.UUID,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    params = {"limit": "3"}
    expected_counts = (20, 2) if merchant_id == MERCHANT_A else (5, 5)
    expected_amounts = [2500, 2500, 5000] if merchant_id == MERCHANT_A else [999_999]

    # Act
    merchant_response = _get(client, RISK, token=_merchant_token(key_pair, merchant_id), params=params)
    ops_response = _get(
        client, OPS_RISK, token=_ops_token(key_pair, role), params={**params, "merchant_id": str(merchant_id)}
    )

    # Assert
    assert (merchant_response.status_code, ops_response.status_code) == (200, 200)
    assert ops_response.headers["Cache-Control"] == "no-store"
    assert _body(ops_response) == _body(merchant_response)
    body = _body(ops_response)
    assert (body["captured_count"], body["chargeback_count"]) == expected_counts
    assert sorted(int(str(item["amount_minor"])) for item in _objs(body["items"])) == expected_amounts


@pytest.mark.parametrize("role", OPS_ROLES)
@pytest.mark.parametrize("path", (OPS_FORECAST, OPS_RISK))
@pytest.mark.parametrize("merchant_id", (None, "not-a-uuid"), ids=("missing", "non_uuid"))
def test_ops_merchant_routes_reject_missing_or_non_uuid_merchant_id_with_422(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    role: OpsRole,
    path: str,
    merchant_id: str | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    params = {} if merchant_id is None else {"merchant_id": merchant_id}

    # Act
    response = _get(client, path, token=_ops_token(key_pair, role), params=params)

    # Assert
    assert response.status_code == 422
    body = _body(response)
    assert set(body) == {"error", "detail"}
    assert body["error"] == "validation_failed"
    [error] = _objs(body["detail"])
    assert error["loc"] == ["query", "merchant_id"]
    assert error["type"] == ("missing" if merchant_id is None else "uuid_parsing")


@pytest.mark.parametrize("role", OPS_ROLES)
def test_ops_forecast_for_an_unknown_merchant_returns_the_empty_merchant_shape(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments_seeded: None, role: OpsRole
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(
        client, OPS_FORECAST, token=_ops_token(key_pair, role), params={"merchant_id": str(UNKNOWN_MERCHANT)}
    )
    merchant_response = _get(client, FORECAST, token=_merchant_token(key_pair, UNKNOWN_MERCHANT))

    # Assert
    assert (response.status_code, merchant_response.status_code) == (200, 200)
    assert response.headers["Cache-Control"] == "no-store"
    body = _body(response)
    assert body == _body(merchant_response)
    assert set(body) == {"generated_at", "horizon_days", "history_days", "currencies"}
    assert _at(body["generated_at"]) == TEST_NOW
    assert (body["horizon_days"], body["history_days"], body["currencies"]) == (7, 28, [])


@pytest.mark.parametrize("role", OPS_ROLES)
def test_ops_chargeback_risk_for_an_unknown_merchant_returns_the_empty_merchant_shape(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments_seeded: None, role: OpsRole
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(
        client, OPS_RISK, token=_ops_token(key_pair, role), params={"merchant_id": str(UNKNOWN_MERCHANT)}
    )
    merchant_response = _get(client, RISK, token=_merchant_token(key_pair, UNKNOWN_MERCHANT))

    # Assert
    assert (response.status_code, merchant_response.status_code) == (200, 200)
    assert response.headers["Cache-Control"] == "no-store"
    body = _body(response)
    assert body == _body(merchant_response)
    assert set(body) == RISK_KEYS
    assert set(_as_obj(body["model"])) == MODEL_KEYS
    assert (body["captured_count"], body["chargeback_count"], body["items"]) == (0, 0, [])
    assert body["merchant_chargeback_rate"] == PRIOR_RATE


@pytest.mark.parametrize("role", OPS_ROLES)
def test_require_ops_principal_returns_the_ops_actor_for_both_roles(role: OpsRole) -> None:
    # Arrange
    principal = OpsScope(ops_user_id=OPS_USER, role=role)

    # Act
    access = require_ops_principal(principal)

    # Assert
    assert access == OpsAccess(actor=f"ops:{OPS_USER}")


@pytest.mark.parametrize(
    "principal",
    (
        Scope(merchant_id=MERCHANT_A),
        Scope(merchant_id=MERCHANT_A, role="merchant_support"),
        UserScope(user_id=USER),
    ),
    ids=("merchant_admin", "merchant_support", "user"),
)
def test_require_ops_principal_keeps_merchant_and_user_principals_forbidden(principal: Principal) -> None:
    # Arrange
    guard = require_ops_principal

    # Act
    with pytest.raises(ForbiddenOpsError) as error:
        guard(principal)

    # Assert
    assert type(error.value) is ForbiddenOpsError


@pytest.mark.parametrize("path", OPS_ROUTES)
@pytest.mark.parametrize("token", (None, "garbage-token-canary-7731"), ids=("no_token", "garbage_token"))
def test_ops_routes_reject_missing_or_garbage_tokens_with_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str, token: str | None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=token, params=_ops_params(path))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize("role", OPS_ROLES)
@pytest.mark.parametrize("path", OPS_ROUTES)
def test_ops_routes_reject_expired_ops_tokens_with_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: OpsRole, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    token = _ops_token(key_pair, role, expired=True)

    # Act
    response = _get(client, path, token=token, params=_ops_params(path))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize("path", OPS_ROUTES)
@pytest.mark.parametrize("principal_kind", ("merchant", "user"))
def test_ops_routes_keep_merchant_and_user_tokens_forbidden(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    payments_seeded: None,
    path: str,
    principal_kind: str,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    token = _merchant_token(key_pair, MERCHANT_A) if principal_kind == "merchant" else _user_token(key_pair)

    # Act
    response = _get(client, path, token=token, params=_ops_params(path, MERCHANT_B))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


@pytest.mark.parametrize("role", OPS_ROLES)
@pytest.mark.parametrize("path", MERCHANT_ROUTES)
def test_merchant_analytics_routes_keep_both_ops_roles_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, role: OpsRole, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    token = _ops_token(key_pair, role)

    # Act
    response = _get(client, path, token=token)

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
