import functools
import uuid
from collections.abc import Callable

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import MAX_AMOUNT_MINOR, CardSummary, ChargeResponse
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

VALID_CARD = {"number": "4242424242424242", "exp_month": 12, "exp_year": 2030, "cvc": "123"}


def _charge_body(number: str = "4242424242424242") -> dict[str, object]:
    return {
        "amount_minor": 1500,
        "currency": "USD",
        "method": "card",
        "card": {**VALID_CARD, "number": number},
    }


def _client(session_factory: sessionmaker[Session], clock: FixedClock) -> TestClient:
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return TestClient(create_app(GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _merchant_auth(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[uuid.UUID, dict[str, str]]:
    with session_factory() as session:
        credentials = create_merchant(session, clock, "Acme", TEST_API_KEY_PEPPER)
        session.commit()
    return credentials.merchant_id, {"Authorization": f"Bearer {credentials.api_key}"}


def test_healthz_returns_200(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200


def test_signup_returns_201_with_user_and_wallet_ids(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.post("/v1/signup", json={"email": "alice@example.com", "password": "correct horse"})

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert set(body) == {"user_id", "wallet_account_id"}
    with session_factory() as session:
        wallet_ids = (
            session.execute(text("SELECT id FROM gateway.ledger_accounts WHERE kind = 'wallet'"))
            .scalars()
            .all()
        )
        user_ids = session.execute(text("SELECT id FROM gateway.users")).scalars().all()
    assert wallet_ids == [uuid.UUID(body["wallet_account_id"])]
    assert user_ids == [uuid.UUID(body["user_id"])]


def test_signup_with_a_duplicate_email_returns_409(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    client.post("/v1/signup", json={"email": "alice@example.com", "password": "correct horse"})

    # Act
    response = client.post("/v1/signup", json={"email": "Alice@Example.com", "password": "another one"})

    # Assert
    assert response.status_code == 409


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_unknown"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_payments_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.post(
        "/v1/payments", json=_charge_body(), headers={**headers, "Idempotency-Key": "key-401"}
    )

    # Assert
    assert response.status_code == 401


def test_payments_without_an_idempotency_key_returns_400(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)

    # Act
    response = client.post("/v1/payments", json=_charge_body(), headers=auth)

    # Assert
    assert response.status_code == 400


@pytest.mark.parametrize(
    "number", ["4242424242424241", "4111111111111111"], ids=["non_luhn", "not_allowlisted"]
)
def test_payments_with_an_invalid_card_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, number: str
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)

    # Act
    response = client.post(
        "/v1/payments", json=_charge_body(number), headers={**auth, "Idempotency-Key": "key-422"}
    )

    # Assert
    assert response.status_code == 422


def test_payments_with_a_valid_request_returns_201_and_the_charge_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)

    # Act
    response = client.post("/v1/payments", json=_charge_body(), headers={**auth, "Idempotency-Key": "key-ok"})

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == ChargeResponse(
        payment_id=uuid.UUID(body["payment_id"]),
        state="succeeded",
        amount_minor=1500,
        currency="USD",
        method="card",
        card=CardSummary(bin="424242", last4="4242"),
        decline_code=None,
    ).model_dump(mode="json")


def test_payments_repeat_with_the_same_key_returns_the_same_body(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)
    headers = {**auth, "Idempotency-Key": "key-repeat"}
    first = client.post("/v1/payments", json=_charge_body(), headers=headers)

    # Act
    second = client.post("/v1/payments", json=_charge_body(), headers=headers)

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json() == first.json()
    with session_factory() as session:
        payment_count = session.execute(text("SELECT count(*) FROM gateway.payments")).scalar_one()
        simulator_count = session.execute(text("SELECT count(*) FROM simulator.charges")).scalar_one()
    assert (payment_count, simulator_count) == (1, 1)


# --- H3: concurrent duplicate sign-up over HTTP ---


def _precheck_misses(*_args: object, **_kwargs: object) -> None:
    return None


def test_signup_race_on_the_email_unique_constraint_returns_409_email_already_registered(
    session_factory: sessionmaker[Session], clock: FixedClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    first = client.post("/v1/signup", json={"email": "alice@example.com", "password": "correct horse"})
    monkeypatch.setattr(Session, "scalar", _precheck_misses)

    # Act
    response = client.post("/v1/signup", json={"email": "alice@example.com", "password": "another one"})

    # Assert
    assert first.status_code == 201
    assert response.status_code == 409
    assert response.json() == {"detail": "email already registered"}
    with session_factory() as session:
        emails = session.execute(text("SELECT email FROM gateway.users")).scalars().all()
    assert emails == ["alice@example.com"]


# --- H5: amount upper bound over HTTP ---


def test_payments_with_an_amount_beyond_int64_returns_422_validation_failed_without_echoing_the_pan(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)
    body = {**_charge_body(), "amount_minor": 2**63}

    # Act
    response = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": "key-huge"})

    # Assert
    payload = response.json()
    assert response.status_code == 422
    assert payload["error"] == "validation_failed"
    assert [error["loc"] for error in payload["detail"]] == [["body", "amount_minor"]]
    assert VALID_CARD["number"] not in response.text
    with session_factory() as session:
        payment_count = session.execute(text("SELECT count(*) FROM gateway.payments")).scalar_one()
    assert payment_count == 0


def test_payments_with_an_amount_just_above_the_maximum_returns_422_validation_failed(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)
    body = {**_charge_body(), "amount_minor": MAX_AMOUNT_MINOR + 1}

    # Act
    response = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": "key-max-plus-one"})

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"


def test_payments_with_an_amount_equal_to_the_maximum_returns_201(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)
    _, auth = _merchant_auth(session_factory, clock)
    body = {**_charge_body(), "amount_minor": MAX_AMOUNT_MINOR}

    # Act
    response = client.post("/v1/payments", json=body, headers={**auth, "Idempotency-Key": "key-max"})

    # Assert
    assert response.status_code == 201
    assert response.json()["amount_minor"] == MAX_AMOUNT_MINOR
    assert response.json()["state"] == "succeeded"


# --- R2: merchant tokens and readiness ---


@functools.cache
def _jwt_private_key_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _public_key_pem(private_key_pem: str) -> str:
    private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    return (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _ready() -> list[str]:
    return []


def _runtime_client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    *,
    jwt_private_key_pem: str | None,
    readiness: Callable[[], list[str]] = _ready,
) -> TestClient:
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    app_deps = GatewayAppDeps(
        charge=deps,
        api_key_pepper=TEST_API_KEY_PEPPER,
        jwt_private_key_pem=jwt_private_key_pem,
        readiness=readiness,
    )
    return TestClient(create_app(app_deps))


def _unverified_claims(token: str) -> dict[str, object]:
    return jwt.decode(token, options={"verify_signature": False})


def test_merchant_tokens_without_a_configured_private_key_returns_503(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=None)
    _, auth = _merchant_auth(session_factory, clock)

    # Act
    response = client.post("/v1/merchant-tokens", headers=auth)

    # Assert
    assert response.status_code == 503
    assert response.json() == {"detail": "token issuing not configured"}


def test_merchant_tokens_returns_201_with_a_bearer_token_that_expires_in_900_seconds(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    private_key_pem = _jwt_private_key_pem()
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=private_key_pem)
    merchant_id, auth = _merchant_auth(session_factory, clock)

    # Act
    response = client.post("/v1/merchant-tokens", headers=auth)

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {"access_token": body.get("access_token"), "token_type": "Bearer", "expires_in": 900}
    claims = jwt.decode(
        body["access_token"],
        _public_key_pem(private_key_pem),
        algorithms=["RS256"],
        audience="ops-ai",
        issuer="gateway",
        options={"verify_exp": False, "verify_iat": False},
    )
    assert claims["merchant_id"] == str(merchant_id)
    assert claims["roles"] == ["merchant_admin"]
    assert claims["iat"] == int(clock().timestamp())
    assert claims["exp"] == int(clock().timestamp()) + 900


def test_merchant_tokens_issues_the_token_for_the_merchant_that_owns_the_api_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=_jwt_private_key_pem())
    merchant_a, auth_a = _merchant_auth(session_factory, clock)
    merchant_b, auth_b = _merchant_auth(session_factory, clock)

    # Act
    token_a = client.post("/v1/merchant-tokens", headers=auth_a).json()["access_token"]
    token_b = client.post("/v1/merchant-tokens", headers=auth_b).json()["access_token"]

    # Assert
    assert merchant_a != merchant_b
    assert _unverified_claims(token_a)["merchant_id"] == str(merchant_a)
    assert _unverified_claims(token_b)["merchant_id"] == str(merchant_b)


def test_merchant_tokens_issues_a_distinct_token_on_each_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=_jwt_private_key_pem())
    _, auth = _merchant_auth(session_factory, clock)

    # Act
    first = client.post("/v1/merchant-tokens", headers=auth).json()["access_token"]
    second = client.post("/v1/merchant-tokens", headers=auth).json()["access_token"]

    # Assert
    assert _unverified_claims(first)["jti"] != _unverified_claims(second)["jti"]


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_leakcanary9f3a"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_merchant_tokens_without_a_valid_api_key_returns_401_and_does_not_echo_the_key(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=_jwt_private_key_pem())

    # Act
    response = client.post("/v1/merchant-tokens", headers=headers)

    # Assert
    assert response.status_code == 401
    assert "leakcanary9f3a" not in response.text
    assert "access_token" not in response.text


def test_readyz_returns_200_ready_with_the_default_readiness(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _client(session_factory, clock)

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


@pytest.mark.parametrize(
    "failed",
    [["database"], ["redis"], ["database", "redis"]],
    ids=["database", "redis", "both"],
)
def test_readyz_returns_503_not_ready_listing_the_failed_components(
    session_factory: sessionmaker[Session], clock: FixedClock, failed: list[str]
) -> None:
    # Arrange
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=None, readiness=lambda: list(failed))

    # Act
    response = client.get("/readyz")

    # Assert
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": failed}


def test_readyz_evaluates_the_readiness_callable_on_every_request(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    outcomes = iter([["redis"], []])
    client = _runtime_client(
        session_factory, clock, jwt_private_key_pem=None, readiness=lambda: next(outcomes)
    )

    # Act
    first = client.get("/readyz")
    second = client.get("/readyz")

    # Assert
    assert (first.status_code, first.json()) == (503, {"status": "not_ready", "failed": ["redis"]})
    assert (second.status_code, second.json()) == (200, {"status": "ready"})


def test_healthz_stays_200_while_readiness_reports_failures(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _runtime_client(
        session_factory, clock, jwt_private_key_pem=None, readiness=lambda: ["database", "redis"]
    )

    # Act
    response = client.get("/healthz")

    # Assert
    assert response.status_code == 200


# --- security-audit follow-up: token responses are uncacheable, secrets stay out of repr ---


def test_merchant_tokens_201_response_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    client = _runtime_client(session_factory, clock, jwt_private_key_pem=_jwt_private_key_pem())
    _, auth = _merchant_auth(session_factory, clock)

    # Act
    response = client.post("/v1/merchant-tokens", headers=auth)

    # Assert
    assert response.status_code == 201
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_gateway_app_deps_repr_does_not_leak_the_pepper_or_the_private_key(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    pepper = "pepper-leakcanary-7c1d4e"
    private_key_pem = _jwt_private_key_pem()
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(charge=charge, api_key_pepper=pepper, jwt_private_key_pem=private_key_pem)
    pem_body_lines = [line for line in private_key_pem.splitlines() if not line.startswith("-----")]

    # Act
    rendered = repr(deps)

    # Assert
    assert rendered.startswith("GatewayAppDeps(")
    assert pepper not in rendered
    assert private_key_pem not in rendered
    assert repr(private_key_pem)[1:-1] not in rendered
    assert pem_body_lines
    assert [line for line in pem_body_lines if line in rendered] == []
