"""Tests for the analytics routes and the ops guard, written from docs/specs/analytics.md section 4."""

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
from ops_ai.analytics.access import ForbiddenOpsError, require_ops_principal
from ops_ai.analytics.chargeback import FEATURE_NAMES, PRIOR_RATE, PRIOR_WEIGHT, default_evaluation
from ops_ai.app import OpsAiAppDeps, create_app
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.scope import Principal, Scope, UserScope
from ops_ai.settlement.models import SettlementEntry
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT_A = uuid.UUID("aaaaaaaa-0000-4000-8000-0000000000a1")
MERCHANT_B = uuid.UUID("bbbbbbbb-0000-4000-8000-0000000000b2")
USER = uuid.UUID("cccccccc-0000-4000-8000-0000000000c3")
ROUTE_FORECAST = "/v1/analytics/forecast"
ROUTE_RISK = "/v1/analytics/chargeback-risk"
ROUTE_ALERTS = "/v1/analytics/alerts"
OPS_ALERTS = "/v1/ops/analytics/alerts"
OPS_FORECAST = "/v1/ops/analytics/forecast"
OPS_RISK = "/v1/ops/analytics/chargeback-risk"
MERCHANT_ROUTES = [ROUTE_FORECAST, ROUTE_RISK, ROUTE_ALERTS]
OPS_ROUTES = [OPS_ALERTS, OPS_FORECAST, OPS_RISK]
INVALID_TOKEN_BODY = {"detail": "invalid token"}
FORBIDDEN_BODY = {"detail": "forbidden"}
FORECAST_KEYS = {"generated_at", "horizon_days", "history_days", "currencies"}
CURRENCY_KEYS = {"currency", "alpha", "history", "forecast", "backtest"}
BACKTEST_KEYS = {"horizon_days", "folds", "mae_minor", "wape", "naive_wape"}
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
RISK_ITEM_KEYS = {"payment_id", "amount_minor", "currency", "occurred_at", "probability", "contributions"}
ALERTS_KEYS = {"server_time", "items"}
ALERT_ITEM_KEYS = {
    "alert_id",
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


def _names(value: object) -> list[str]:
    assert isinstance(value, list | dict)
    return [str(name) for name in value]


def _at(value: object) -> datetime:
    assert isinstance(value, str)
    return datetime.fromisoformat(value)


def _ops_params(path: str) -> dict[str, str]:
    return {} if path == OPS_ALERTS else {"merchant_id": str(MERCHANT_A)}


# --- seed data -------------------------------------------------------------------------------------------


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
    """Eight steady buckets (12 payments, 1 declined each), then 10 live declines in the 12:00 bucket."""
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


# --- auth ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", [*MERCHANT_ROUTES, *OPS_ROUTES])
def test_every_analytics_route_without_a_token_returns_401_invalid_token(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, params=_ops_params(path))

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY


@pytest.mark.parametrize("path", MERCHANT_ROUTES)
def test_merchant_routes_reject_a_garbage_token_with_401(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token="garbage-token-canary-7731")

    # Assert
    assert response.status_code == 401
    assert response.json() == INVALID_TOKEN_BODY
    assert "garbage-token-canary-7731" not in response.text


@pytest.mark.parametrize("path", MERCHANT_ROUTES)
def test_merchant_routes_reject_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_user_token(key_pair))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


@pytest.mark.parametrize("path", OPS_ROUTES)
def test_ops_routes_reject_a_merchant_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, alerts_seeded: None, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_merchant_token(key_pair), params=_ops_params(path))

    # Assert: fail-closed, even for the merchant's own id, and no alert data leaks
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY
    assert "alert_id" not in response.text


@pytest.mark.parametrize("path", OPS_ROUTES)
def test_ops_routes_reject_a_user_token_with_403_forbidden(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_user_token(key_pair), params=_ops_params(path))

    # Assert
    assert response.status_code == 403
    assert response.json() == FORBIDDEN_BODY


# --- the ops guard ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "principal",
    [Scope(merchant_id=MERCHANT_A), UserScope(user_id=USER)],
    ids=["merchant_scope", "user_scope"],
)
def test_require_ops_principal_raises_forbidden_for_every_current_principal(principal: Principal) -> None:
    # Arrange / Act / Assert
    with pytest.raises(ForbiddenOpsError):
        require_ops_principal(principal)


# --- merchant routes: shape ------------------------------------------------------------------------------


@pytest.mark.parametrize("path", MERCHANT_ROUTES)
def test_merchant_routes_return_200_with_cache_control_no_store(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_merchant_token(key_pair))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_forecast_route_without_data_returns_the_specified_keys_and_defaults(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    body = _body(_get(client, ROUTE_FORECAST, token=_merchant_token(key_pair)))

    # Assert
    assert set(body) == FORECAST_KEYS
    assert (body["horizon_days"], body["history_days"], body["currencies"]) == (7, 28, [])
    assert _at(body["generated_at"]) == TEST_NOW


def test_chargeback_risk_route_without_data_returns_the_specified_keys_and_the_prior_rate(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    body = _body(_get(client, ROUTE_RISK, token=_merchant_token(key_pair)))

    # Assert
    assert set(body) == RISK_KEYS
    assert set(_as_obj(body["model"])) == MODEL_KEYS
    assert (body["captured_count"], body["chargeback_count"], body["items"]) == (0, 0, [])
    assert body["merchant_chargeback_rate"] == pytest.approx(PRIOR_RATE)


def test_alerts_route_without_data_returns_the_specified_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    body = _body(_get(client, ROUTE_ALERTS, token=_merchant_token(key_pair)))

    # Assert
    assert set(body) == ALERTS_KEYS
    assert body["items"] == []
    assert _at(body["server_time"]) == TEST_NOW


# --- merchant routes: parameters -------------------------------------------------------------------------


@pytest.mark.parametrize("path", MERCHANT_ROUTES)
def test_merchant_routes_reject_a_merchant_id_query_parameter_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments_seeded: None, path: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_merchant_token(key_pair), params={"merchant_id": str(MERCHANT_B)})

    # Assert
    assert response.status_code == 422
    assert "999999" not in response.text


@pytest.mark.parametrize(
    ("path", "param", "value"),
    [
        (ROUTE_FORECAST, "horizon_days", "0"),
        (ROUTE_FORECAST, "horizon_days", "31"),
        (ROUTE_FORECAST, "horizon_days", "-1"),
        (ROUTE_FORECAST, "horizon_days", "abc"),
        (ROUTE_RISK, "limit", "0"),
        (ROUTE_RISK, "limit", "101"),
        (ROUTE_RISK, "limit", "abc"),
        (ROUTE_ALERTS, "limit", "0"),
        (ROUTE_ALERTS, "limit", "201"),
        (ROUTE_ALERTS, "since", "yesterday"),
    ],
)
def test_merchant_routes_reject_out_of_range_or_malformed_parameters_with_422(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str, param: str, value: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_merchant_token(key_pair), params={param: value})

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "param", "value"),
    [
        (ROUTE_FORECAST, "horizon_days", "1"),
        (ROUTE_FORECAST, "horizon_days", "30"),
        (ROUTE_RISK, "limit", "1"),
        (ROUTE_RISK, "limit", "100"),
        (ROUTE_ALERTS, "limit", "1"),
        (ROUTE_ALERTS, "limit", "200"),
        (ROUTE_ALERTS, "since", "2026-11-03T11:00:00+00:00"),
    ],
)
def test_merchant_routes_accept_the_boundary_parameter_values(
    session_factory: sessionmaker[Session], key_pair: KeyPair, path: str, param: str, value: str
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, path, token=_merchant_token(key_pair), params={param: value})

    # Assert
    assert response.status_code == 200


# --- forecast route: data --------------------------------------------------------------------------------


def test_forecast_route_returns_the_merchants_daily_history_and_the_requested_horizon(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    response = _get(client, ROUTE_FORECAST, token=_merchant_token(key_pair), params={"horizon_days": "3"})

    # Assert: merchant B's 999999 never shows up; 2026-11-01 holds 2500, 2026-11-02 holds 5000 + 2500
    body = _body(response)
    [usd] = _objs(body["currencies"])
    assert set(usd) == CURRENCY_KEYS
    assert usd["currency"] == "USD"
    history = _objs(usd["history"])
    assert len(history) == 28
    assert set(history[0]) == {"day", "amount_minor"}
    assert history[-2:] == [
        {"day": "2026-11-01", "amount_minor": 2500},
        {"day": "2026-11-02", "amount_minor": 7500},
    ]
    forecast = _objs(usd["forecast"])
    assert [p["day"] for p in forecast] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert all(set(p) == {"day", "expected_minor", "low_minor", "high_minor"} for p in forecast)
    assert body["horizon_days"] == 3
    assert "999999" not in response.text


@pytest.mark.parametrize(
    ("horizon_days", "folds"),
    [(7, 3), (14, 1), (30, None)],
    ids=["h7_three_folds", "h14_one_fold", "h30_none"],
)
def test_forecast_route_backtest_has_the_specified_keys_and_skips_folds_with_too_little_training(
    session_factory: sessionmaker[Session],
    key_pair: KeyPair,
    payments_seeded: None,
    horizon_days: int,
    folds: int | None,
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    body = _body(
        _get(
            client,
            ROUTE_FORECAST,
            token=_merchant_token(key_pair),
            params={"horizon_days": str(horizon_days)},
        )
    )

    # Assert: 28 history days, folds train on 28 - k * horizon points and need at least 7
    [usd] = _objs(body["currencies"])
    if folds is None:
        assert usd["backtest"] is None
    else:
        backtest = _as_obj(usd["backtest"])
        assert set(backtest) == BACKTEST_KEYS
        assert (backtest["horizon_days"], backtest["folds"]) == (horizon_days, folds)


# --- chargeback-risk route: data -------------------------------------------------------------------------


def test_chargeback_risk_route_scores_the_merchants_payments_with_the_default_model(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    evaluation = default_evaluation()

    # Act
    response = _get(client, ROUTE_RISK, token=_merchant_token(key_pair))

    # Assert
    body = _body(response)
    assert (body["captured_count"], body["chargeback_count"]) == (20, 2)
    expected_rate = (2 + PRIOR_WEIGHT * PRIOR_RATE) / (20 + PRIOR_WEIGHT)
    assert body["merchant_chargeback_rate"] == pytest.approx(expected_rate)
    model = _as_obj(body["model"])
    assert set(model) == MODEL_KEYS
    assert set(_names(model["features"])) == set(FEATURE_NAMES)
    assert (model["train_count"], model["test_count"], model["test_positives"]) == (
        evaluation.train_count,
        evaluation.test_count,
        evaluation.test_positives,
    )
    assert model["brier"] == pytest.approx(evaluation.brier)
    assert model["base_rate_brier"] == pytest.approx(evaluation.base_rate_brier)
    if evaluation.auc is None:
        assert model["auc"] is None
    else:
        assert model["auc"] == pytest.approx(evaluation.auc)
    items = _objs(body["items"])
    assert {item["payment_id"] for item in items} == {str(uuid.UUID(int=n)) for n in (1, 2, 3)}
    assert all(set(item) == RISK_ITEM_KEYS for item in items)
    assert all(set(_as_obj(item["contributions"])) == set(FEATURE_NAMES) for item in items)
    assert "999999" not in response.text


def test_chargeback_risk_route_lists_the_most_probable_payments_first_and_honours_limit(
    session_factory: sessionmaker[Session], key_pair: KeyPair, payments_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    token = _merchant_token(key_pair)

    # Act
    full = _objs(_body(_get(client, ROUTE_RISK, token=token))["items"])
    limited = _objs(_body(_get(client, ROUTE_RISK, token=token, params={"limit": "2"}))["items"])

    # Assert
    probabilities = [float(str(item["probability"])) for item in full]
    assert probabilities == sorted(probabilities, reverse=True)
    assert all(0.0 < p < 1.0 for p in probabilities)
    assert [item["payment_id"] for item in limited] == [item["payment_id"] for item in full[:2]]


# --- alerts routes: data ---------------------------------------------------------------------------------


def test_alerts_route_returns_the_merchants_own_alerts_with_the_specified_item_keys(
    session_factory: sessionmaker[Session], key_pair: KeyPair, alerts_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    body = _body(_get(client, ROUTE_ALERTS, token=_merchant_token(key_pair, MERCHANT_A)))

    # Assert
    items = _objs(body["items"])
    assert {item["alert_id"] for item in items} == _stored_alert_ids(session_factory, MERCHANT_A)
    assert all(set(item) == ALERT_ITEM_KEYS for item in items)
    assert sorted(str(item["metric"]) for item in items) == ["decline_mix", "failure_rate"]
    failure = next(item for item in items if item["metric"] == "failure_rate")
    assert failure["detail"] is None
    assert failure["attempts"] == 10
    assert failure["observed"] == pytest.approx(1.0)
    assert _at(failure["bucket_start"]) == TEST_NOW
    assert (_at(failure["created_at"]), _at(failure["updated_at"])) == (CLOCK_A, CLOCK_A)
    mix = next(item for item in items if item["metric"] == "decline_mix")
    assert mix["detail"] == "issuer_unavailable"


def test_alerts_route_never_shows_another_merchants_alerts(
    session_factory: sessionmaker[Session], key_pair: KeyPair, alerts_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    other_ids = _stored_alert_ids(session_factory, MERCHANT_B)

    # Act
    response = _get(client, ROUTE_ALERTS, token=_merchant_token(key_pair, MERCHANT_A))

    # Assert
    assert len(other_ids) == 2
    assert all(alert_id not in response.text for alert_id in other_ids)
    assert str(MERCHANT_B) not in response.text


def test_alerts_route_since_keeps_only_alerts_updated_strictly_after(
    session_factory: sessionmaker[Session], key_pair: KeyPair, alerts_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)
    token = _merchant_token(key_pair, MERCHANT_A)

    # Act
    just_before = {"since": (CLOCK_A - timedelta(seconds=1)).isoformat()}
    earlier = _body(_get(client, ROUTE_ALERTS, token=token, params=just_before))
    at_update = _body(_get(client, ROUTE_ALERTS, token=token, params={"since": CLOCK_A.isoformat()}))

    # Assert
    assert len(_objs(earlier["items"])) == 2
    assert at_update["items"] == []


def test_alerts_route_limit_caps_the_number_of_items(
    session_factory: sessionmaker[Session], key_pair: KeyPair, alerts_seeded: None
) -> None:
    # Arrange
    client = _client(session_factory, key_pair)

    # Act
    token = _merchant_token(key_pair, MERCHANT_A)
    body = _body(_get(client, ROUTE_ALERTS, token=token, params={"limit": "1"}))

    # Assert
    assert len(_objs(body["items"])) == 1
