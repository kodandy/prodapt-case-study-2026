"""HTTP tests for the wallet, top-up, payout and cross-currency transfer routes, written from
docs/specs/gateway-basic.md section B1.4 / B2.3 / B2.5 / P0-2 / P0-3 (Routes).

`RailResult` is imported from the `gateway.payments.rails` package, so its `__init__` must re-export it.
The root conftest truncates every gateway table, which also empties the `fx_rates` rows the migration seeds,
so the FX tests insert the demo rates they need themselves (`INSERT ... ON CONFLICT DO NOTHING`).
"""

import uuid
from dataclasses import replace
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.rails import RailResult
from gateway.wallets.payouts import settle_payout
from gateway.wallets.service import list_wallets, top_up_wallet
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

WALLETS = "/v1/me/wallets"
TOP_UPS = "/v1/me/top-ups"
PAYOUTS = "/v1/me/payouts"
TRANSFERS = "/v1/me/transfers"
SESSIONS = "/v1/sessions"
ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "route-test-password-7Q"
MAX_TOP_UP_MINOR = 10_000_000
MAX_AMOUNT_MINOR = 10**12
DESTINATION = "DE89370400440532013000"
MASKED = "****3000"
NO_STORE = "no-store"
MISSING_SESSION = {"detail": "missing session"}
INVALID_SESSION = {"detail": "invalid session"}
KEY_REQUIRED = {"detail": "Idempotency-Key header is required"}
KEY_INVALID = {"detail": "invalid Idempotency-Key header"}
KEY_REUSED = {"error": "idempotency_key_reused"}
PAYOUT_NOT_FOUND = {"detail": "payout not found"}
DEMO_FX_RATES = (
    ("demo-USDEUR-v1", "USD", "EUR", "0.9200000000"),
    ("demo-USDJPY-v1", "USD", "JPY", "151.5000000000"),
    ("demo-JPYUSD-v1", "JPY", "USD", "0.0066006601"),
)


class _DestinationRail:
    """Answers from the destination: `fail` -> failed, `unknown` -> unknown, `timeout` -> TimeoutError."""

    name = "destination-rail"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int, str, str]] = []

    def submit(
        self, payout_id: str, method: str, amount_minor: int, currency: str, destination: str
    ) -> RailResult:
        self.calls.append((payout_id, method, amount_minor, currency, destination))
        if "timeout" in destination:
            raise TimeoutError("rail timed out")
        if "fail" in destination:
            return RailResult(status="failed", failure_code="rail_rejected")
        if "unknown" in destination:
            return RailResult(status="unknown")
        return RailResult(status="succeeded")


def _generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def private_pem() -> str:
    return _generate_private_pem()


@pytest.fixture
def rail() -> _DestinationRail:
    return _DestinationRail()


def _client(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail | None = None,
    demo_top_up_enabled: bool | None = None,
) -> TestClient:
    charge = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    deps = GatewayAppDeps(
        charge=charge, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=private_pem, rail=rail
    )
    if demo_top_up_enabled is not None:
        deps = replace(deps, demo_top_up_enabled=demo_top_up_enabled)
    return TestClient(create_app(deps))


def _sign_up(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _fund(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    amount_minor: int,
    currency: str = "USD",
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, currency, amount_minor)


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _count(session_factory: sessionmaker[Session], query: str) -> int:
    with session_factory() as session:
        return int(str(session.execute(text(query)).scalar_one()))


def _seed_fx(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        for rate_id, base, quote, rate in DEMO_FX_RATES:
            session.execute(
                text(
                    "INSERT INTO gateway.fx_rates (id, base_currency, quote_currency, rate, valid_from) "
                    "VALUES (:id, :base, :quote, :rate, '2026-01-01T00:00:00+00:00') ON CONFLICT DO NOTHING"
                ),
                {"id": rate_id, "base": base, "quote": quote, "rate": Decimal(rate)},
            )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(private_pem: str, clock: FixedClock, user_id: uuid.UUID) -> str:
    return issue_session_token(private_pem, user_id, clock(), ("pwd",))


def _post(
    client: TestClient, path: str, token: str | None, body: object, key: str | None = "k-1"
) -> Response:
    headers = {} if token is None else _bearer(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    response: Response = client.post(path, headers=headers, json=body)
    return response


def _get(client: TestClient, path: str, token: str | None) -> Response:
    response: Response = client.get(path, headers={} if token is None else _bearer(token))
    return response


def _top_up_body(currency: object = "USD", amount_minor: object = 5_000) -> dict[str, object]:
    return {"currency": currency, "amount_minor": amount_minor}


def _payout_body(
    method: object = "bank_transfer",
    amount_minor: object = 2_500,
    currency: object = "USD",
    destination: object = DESTINATION,
) -> dict[str, object]:
    return {"method": method, "amount_minor": amount_minor, "currency": currency, "destination": destination}


def _payout_view(payout_id: object, state: str, failure_code: str | None = None) -> dict[str, object]:
    return {
        "payout_id": payout_id,
        "state": state,
        "method": "bank_transfer",
        "amount_minor": 2_500,
        "currency": "USD",
        "destination_masked": MASKED,
        "failure_code": failure_code,
    }


def _transfer_body(
    amount_minor: object = 10_000, currency: object = "USD", recipient_currency: object = "EUR"
) -> dict[str, object]:
    return {
        "recipient_email": BOB,
        "amount_minor": amount_minor,
        "currency": currency,
        "recipient_currency": recipient_currency,
    }


def _funded_alice(
    session_factory: sessionmaker[Session], clock: FixedClock, amount_minor: int = 10_000
) -> uuid.UUID:
    alice = _sign_up(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, amount_minor)
    return alice


# --- POST /v1/me/wallets ----------------------------------------------------------------------------------


def test_post_wallets_creates_a_wallet_with_201_then_answers_200_for_the_same_currency(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    token = _token(private_pem, clock, alice)

    # Act
    first = _post(client, WALLETS, token, {"currency": "EUR"}, key=None)
    second = _post(client, WALLETS, token, {"currency": "EUR"}, key=None)

    # Assert
    assert first.status_code == 201
    assert first.json() == {
        "account_id": str(uuid.UUID(first.json()["account_id"])),
        "currency": "EUR",
        "available_minor": 0,
        "held_minor": 0,
    }
    assert (second.status_code, second.json()) == (200, first.json())
    assert _wallets(session_factory, alice) == [("EUR", 0, 0), ("USD", 0, 0)]


def test_post_wallets_for_the_signup_currency_answers_200_with_the_current_balance(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_alice(session_factory, clock, 4_200)

    # Act
    response = _post(client, WALLETS, _token(private_pem, clock, alice), {"currency": "USD"}, key=None)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert (body["currency"], body["available_minor"], body["held_minor"]) == ("USD", 4_200, 0)


@pytest.mark.parametrize("currency", ["EUR", "USD"], ids=["created_201", "existing_200"])
def test_post_wallets_answers_carry_cache_control_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, currency: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, WALLETS, _token(private_pem, clock, alice), {"currency": currency}, key=None)

    # Assert
    assert response.headers["Cache-Control"] == NO_STORE


@pytest.mark.parametrize(
    "body",
    [
        {"currency": "GBP"},
        {"currency": "usd"},
        {"currency": ""},
        {"currency": 840},
        {},
        {"currency": "EUR", "available_minor": 100},
    ],
    ids=["gbp", "lowercase", "empty", "number", "missing_currency", "extra_field"],
)
def test_post_wallets_with_an_invalid_body_returns_422_and_creates_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, WALLETS, _token(private_pem, clock, alice), body, key=None)

    # Assert
    assert response.status_code == 422
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]


def test_post_wallets_without_a_session_returns_401_missing_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)

    # Act
    response = _post(client, WALLETS, None, {"currency": "EUR"}, key=None)

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)


def test_post_wallets_with_a_bad_token_returns_401_invalid_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)

    # Act
    response = _post(client, WALLETS, "not-a-jwt", {"currency": "EUR"}, key=None)

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_SESSION)


def test_post_wallets_works_with_a_token_from_post_sessions(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    login = client.post(SESSIONS, json={"email": ALICE, "password": PASSWORD})

    # Act
    response = _post(client, WALLETS, str(login.json()["access_token"]), {"currency": "INR"}, key=None)

    # Assert
    assert login.status_code == 201
    assert response.status_code == 201
    assert response.json()["currency"] == "INR"
    assert _wallets(session_factory, alice) == [("INR", 0, 0), ("USD", 0, 0)]


def test_a_user_only_creates_wallets_for_themselves(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)

    # Act
    response = _post(client, WALLETS, _token(private_pem, clock, alice), {"currency": "JPY"}, key=None)

    # Assert
    assert response.status_code == 201
    assert _wallets(session_factory, alice) == [("JPY", 0, 0), ("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]


# --- POST /v1/me/top-ups ----------------------------------------------------------------------------------


def test_post_top_ups_is_disabled_by_default_and_returns_404_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body())

    # Assert
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]


def test_post_top_ups_is_404_when_explicitly_disabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=False)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body())

    # Assert
    assert (response.status_code, response.json()) == (404, {"detail": "Not Found"})
    assert _count(session_factory, "SELECT count(*) FROM gateway.wallet_top_ups") == 0


def test_post_top_ups_when_enabled_returns_201_with_the_top_up_body_and_credits_the_wallet(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body())

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "top_up_id": str(uuid.UUID(body["top_up_id"])),
        "currency": "USD",
        "amount_minor": 5_000,
        "available_minor": 5_000,
    }
    assert response.headers["Cache-Control"] == NO_STORE
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]


def test_post_top_ups_creates_the_wallet_for_a_new_currency(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body("JPY", 15_000))

    # Assert
    assert response.status_code == 201
    assert response.json()["available_minor"] == 15_000
    assert _wallets(session_factory, alice) == [("JPY", 15_000, 0), ("USD", 0, 0)]


def test_post_top_ups_replay_returns_the_same_201_body_without_a_second_credit(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)
    token = _token(private_pem, clock, alice)
    first = _post(client, TOP_UPS, token, _top_up_body())

    # Act
    replay = _post(client, TOP_UPS, token, _top_up_body())

    # Assert
    assert (replay.status_code, replay.json()) == (201, first.json())
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]
    assert _count(session_factory, "SELECT count(*) FROM gateway.wallet_top_ups") == 1


def test_post_top_ups_with_the_same_key_and_a_different_body_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)
    token = _token(private_pem, clock, alice)
    _post(client, TOP_UPS, token, _top_up_body())

    # Act
    response = _post(client, TOP_UPS, token, _top_up_body(amount_minor=5_001))

    # Assert
    assert (response.status_code, response.json()) == (422, KEY_REUSED)
    assert response.headers["Cache-Control"] == NO_STORE
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]


def test_post_top_ups_without_an_idempotency_key_returns_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body(), key=None)

    # Assert
    assert (response.status_code, response.json()) == (400, KEY_REQUIRED)
    assert response.headers["Cache-Control"] == NO_STORE
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]


def test_post_top_ups_with_an_invalid_idempotency_key_returns_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body(), key="a b")

    # Assert
    assert (response.status_code, response.json()) == (400, KEY_INVALID)


def test_post_top_ups_accepts_exactly_max_top_up_minor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(
        client, TOP_UPS, _token(private_pem, clock, alice), _top_up_body(amount_minor=MAX_TOP_UP_MINOR)
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["available_minor"] == MAX_TOP_UP_MINOR
    assert _wallets(session_factory, alice) == [("USD", MAX_TOP_UP_MINOR, 0)]


@pytest.mark.parametrize(
    "body",
    [
        _top_up_body(amount_minor=MAX_TOP_UP_MINOR + 1),
        _top_up_body(amount_minor=0),
        _top_up_body(amount_minor=-1),
        _top_up_body(amount_minor=10.5),
        _top_up_body(amount_minor="100"),
        _top_up_body(amount_minor=True),
        _top_up_body(amount_minor=None),
        _top_up_body(currency="GBP"),
        _top_up_body(currency=None),
        {"currency": "USD"},
        {"amount_minor": 100},
        {**_top_up_body(), "user_id": "someone-else"},
    ],
    ids=[
        "above_max",
        "zero",
        "negative",
        "float",
        "numeric_string",
        "bool",
        "null_amount",
        "gbp",
        "null_currency",
        "missing_amount",
        "missing_currency",
        "extra_field",
    ],
)
def test_post_top_ups_with_an_invalid_body_returns_422_and_credits_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, body: dict[str, object]
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _post(client, TOP_UPS, _token(private_pem, clock, alice), body)

    # Assert
    assert response.status_code == 422
    assert _wallets(session_factory, alice) == [("USD", 0, 0)]
    assert _count(session_factory, "SELECT count(*) FROM gateway.journal_entries") == 0


def test_post_top_ups_without_a_session_returns_401_missing_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, demo_top_up_enabled=True)

    # Act
    response = _post(client, TOP_UPS, None, _top_up_body())

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)


# --- POST /v1/me/payouts ----------------------------------------------------------------------------------


def test_post_payouts_returns_201_succeeded_when_the_rail_accepts(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(client, PAYOUTS, _token(private_pem, clock, alice), _payout_body())

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == _payout_view(str(uuid.UUID(body["payout_id"])), "succeeded")
    assert response.headers["Cache-Control"] == NO_STORE
    assert rail.calls == [(body["payout_id"], "bank_transfer", 2_500, "USD", DESTINATION)]
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]


def test_post_payouts_returns_201_failed_with_the_failure_code_and_restores_the_balance(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(
        client, PAYOUTS, _token(private_pem, clock, alice), _payout_body(destination="acct-fail-0042")
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body["state"] == "failed"
    assert body["failure_code"] == "rail_rejected"
    assert response.headers["Cache-Control"] == NO_STORE
    assert _wallets(session_factory, alice) == [("USD", 10_000, 0)]


def test_post_payouts_returns_202_unknown_and_keeps_the_funds_held(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(
        client, PAYOUTS, _token(private_pem, clock, alice), _payout_body(destination="acct-unknown-0042")
    )

    # Assert
    body = response.json()
    assert response.status_code == 202
    assert body == _payout_view(body["payout_id"], "unknown") | {"destination_masked": "****0042"}
    assert response.headers["Cache-Control"] == NO_STORE
    assert _wallets(session_factory, alice) == [("USD", 7_500, 2_500)]


def test_post_payouts_treats_a_rail_timeout_as_202_unknown(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(
        client, PAYOUTS, _token(private_pem, clock, alice), _payout_body(destination="acct-timeout-0042")
    )

    # Assert
    assert response.status_code == 202
    assert response.json()["state"] == "unknown"
    assert _wallets(session_factory, alice) == [("USD", 7_500, 2_500)]


def test_post_payouts_via_upi_masks_the_destination(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(
        client,
        PAYOUTS,
        _token(private_pem, clock, alice),
        _payout_body(method="upi", destination="alice.pay@okbank"),
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert (body["method"], body["destination_masked"]) == ("upi", "****bank")
    assert "alice.pay@okbank" not in response.text


def test_post_payouts_never_echoes_the_raw_destination_or_stores_it(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(client, PAYOUTS, _token(private_pem, clock, alice), _payout_body())

    # Assert
    with session_factory() as session:
        stored = session.execute(text("SELECT p::text FROM gateway.payouts p")).scalars().all()
    assert DESTINATION not in response.text
    assert [DESTINATION in row for row in stored] == [False]


def test_post_payouts_above_the_available_balance_returns_409_and_moves_nothing(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock, 1_000)

    # Act
    response = _post(client, PAYOUTS, _token(private_pem, clock, alice), _payout_body(amount_minor=1_001))

    # Assert
    assert (response.status_code, response.json()) == (
        409,
        {"error": "insufficient_funds", "available_minor": 1_000},
    )
    assert response.headers["Cache-Control"] == NO_STORE
    assert rail.calls == []
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _count(session_factory, "SELECT count(*) FROM gateway.payouts") == 0


def test_post_payouts_replay_returns_the_same_answer_without_a_second_hold_or_rail_call(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)
    token = _token(private_pem, clock, alice)
    first = _post(client, PAYOUTS, token, _payout_body())

    # Act
    replay = _post(client, PAYOUTS, token, _payout_body())

    # Assert
    assert (replay.status_code, replay.json()) == (first.status_code, first.json())
    assert len(rail.calls) == 1
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _count(session_factory, "SELECT count(*) FROM gateway.payouts") == 1


def test_post_payouts_with_the_same_key_and_a_different_body_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)
    token = _token(private_pem, clock, alice)
    _post(client, PAYOUTS, token, _payout_body())

    # Act
    response = _post(client, PAYOUTS, token, _payout_body(destination="DE89370400440532019999"))

    # Assert
    assert (response.status_code, response.json()) == (422, KEY_REUSED)
    assert response.headers["Cache-Control"] == NO_STORE
    assert len(rail.calls) == 1
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]


def test_post_payouts_without_an_idempotency_key_returns_400(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(client, PAYOUTS, _token(private_pem, clock, alice), _payout_body(), key=None)

    # Assert
    assert (response.status_code, response.json()) == (400, KEY_REQUIRED)
    assert response.headers["Cache-Control"] == NO_STORE
    assert rail.calls == []
    assert _wallets(session_factory, alice) == [("USD", 10_000, 0)]


@pytest.mark.parametrize(
    "body",
    [
        _payout_body(method="wire"),
        _payout_body(method="card"),
        _payout_body(method=None),
        _payout_body(destination="abc"),
        _payout_body(destination="a" * 65),
        _payout_body(destination="bad destination"),
        _payout_body(destination="drop;table"),
        _payout_body(destination="caf\xe9-account"),
        _payout_body(destination=""),
        _payout_body(destination=None),
        _payout_body(destination=12345678),
        _payout_body(amount_minor=0),
        _payout_body(amount_minor=-5),
        _payout_body(amount_minor=MAX_AMOUNT_MINOR + 1),
        _payout_body(amount_minor=10.5),
        _payout_body(amount_minor="100"),
        _payout_body(amount_minor=True),
        _payout_body(currency="GBP"),
        {**_payout_body(), "user_id": "someone-else"},
        {"method": "upi", "amount_minor": 100, "currency": "USD"},
        {"method": "upi", "amount_minor": 100, "destination": DESTINATION},
    ],
    ids=[
        "method_wire",
        "method_card",
        "method_null",
        "destination_3_chars",
        "destination_65_chars",
        "destination_space",
        "destination_semicolon",
        "destination_non_ascii",
        "destination_empty",
        "destination_null",
        "destination_number",
        "amount_zero",
        "amount_negative",
        "amount_above_max",
        "amount_float",
        "amount_string",
        "amount_bool",
        "currency_gbp",
        "extra_field",
        "missing_destination",
        "missing_currency",
    ],
)
def test_post_payouts_with_an_invalid_body_returns_422_and_does_nothing(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
    body: dict[str, object],
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(client, PAYOUTS, _token(private_pem, clock, alice), body)

    # Assert
    assert response.status_code == 422
    assert rail.calls == []
    assert _wallets(session_factory, alice) == [("USD", 10_000, 0)]
    assert _count(session_factory, "SELECT count(*) FROM gateway.payouts") == 0


@pytest.mark.parametrize(
    "destination",
    ["ab12", "a" * 64, "Az09@._:-xyz"],
    ids=["shortest_4", "longest_64", "all_allowed_punctuation"],
)
def test_post_payouts_accepts_destinations_at_the_length_and_character_limits(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
    destination: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)

    # Act
    response = _post(
        client, PAYOUTS, _token(private_pem, clock, alice), _payout_body(destination=destination)
    )

    # Assert
    assert response.status_code == 201
    assert response.json()["destination_masked"] == "****" + destination[-4:]
    assert [call[4] for call in rail.calls] == [destination]


def test_post_payouts_without_a_session_returns_401_missing_session(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)

    # Act
    response = _post(client, PAYOUTS, None, _payout_body())

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)
    assert rail.calls == []


def test_post_payouts_uses_the_simulated_rail_when_no_rail_is_injected(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_alice(session_factory, clock)
    token = _token(private_pem, clock, alice)

    # Act
    accepted = _post(client, PAYOUTS, token, _payout_body(), key="k-ok")
    rejected = _post(client, PAYOUTS, token, _payout_body(destination="acct-fail-0042"), key="k-fail")
    unknown = _post(client, PAYOUTS, token, _payout_body(destination="acct-unknown-0042"), key="k-unk")

    # Assert
    assert [r.status_code for r in (accepted, rejected, unknown)] == [201, 201, 202]
    assert [r.json()["state"] for r in (accepted, rejected, unknown)] == ["succeeded", "failed", "unknown"]
    assert _wallets(session_factory, alice) == [("USD", 5_000, 2_500)]


# --- GET /v1/me/payouts/{payout_id} -----------------------------------------------------------------------


def test_get_payout_returns_200_with_the_owners_current_view(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)
    token = _token(private_pem, clock, alice)
    created = _post(client, PAYOUTS, token, _payout_body(destination="acct-unknown-3000"))
    payout_id = created.json()["payout_id"]

    # Act
    response = _get(client, f"{PAYOUTS}/{payout_id}", token)

    # Assert
    assert response.status_code == 200
    assert response.json() == _payout_view(payout_id, "unknown")
    assert response.headers["Cache-Control"] == NO_STORE


def test_get_payout_reflects_a_later_settle(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)
    token = _token(private_pem, clock, alice)
    created = _post(client, PAYOUTS, token, _payout_body(destination="acct-unknown-3000"))
    payout_id = created.json()["payout_id"]
    settle_payout(session_factory, clock, uuid.UUID(payout_id), RailResult(status="succeeded"))

    # Act
    response = _get(client, f"{PAYOUTS}/{payout_id}", token)

    # Assert
    assert response.status_code == 200
    assert response.json() == _payout_view(payout_id, "succeeded")
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]


def test_get_payout_of_another_users_payout_returns_404_payout_not_found(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail)
    alice = _funded_alice(session_factory, clock)
    bob = _sign_up(session_factory, clock, BOB)
    created = _post(client, PAYOUTS, _token(private_pem, clock, alice), _payout_body())

    # Act
    response = _get(client, f"{PAYOUTS}/{created.json()['payout_id']}", _token(private_pem, clock, bob))

    # Assert
    assert (response.status_code, response.json()) == (404, PAYOUT_NOT_FOUND)
    assert response.headers["Cache-Control"] == NO_STORE


def test_get_payout_of_an_unknown_id_returns_404_payout_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _get(client, f"{PAYOUTS}/{uuid.uuid4()}", _token(private_pem, clock, alice))

    # Assert
    assert (response.status_code, response.json()) == (404, PAYOUT_NOT_FOUND)


def test_get_payout_with_a_non_uuid_id_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _get(client, f"{PAYOUTS}/not-a-uuid", _token(private_pem, clock, alice))

    # Assert
    assert response.status_code == 422


def test_get_payout_without_a_session_returns_401_missing_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)

    # Act
    response = _get(client, f"{PAYOUTS}/{uuid.uuid4()}", None)

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)


# --- GET /v1/me/wallets after top-ups and payouts ---------------------------------------------------------


def test_get_wallets_lists_the_created_topped_up_and_held_wallets_ordered_by_currency(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    rail: _DestinationRail,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem, rail=rail, demo_top_up_enabled=True)
    alice = _sign_up(session_factory, clock, ALICE)
    token = _token(private_pem, clock, alice)
    _post(client, WALLETS, token, {"currency": "INR"}, key=None)
    _post(client, TOP_UPS, token, _top_up_body("USD", 10_000), key="top-usd")
    _post(client, TOP_UPS, token, _top_up_body("EUR", 777), key="top-eur")
    _post(client, PAYOUTS, token, _payout_body(destination="acct-unknown-0001"), key="pay-1")
    _post(client, PAYOUTS, token, _payout_body(amount_minor=500), key="pay-2")

    # Act
    response = _get(client, WALLETS, token)

    # Assert
    items = response.json()["items"]
    assert response.status_code == 200
    assert [(i["currency"], i["available_minor"], i["held_minor"]) for i in items] == [
        ("EUR", 777, 0),
        ("INR", 0, 0),
        ("USD", 7_000, 2_500),
    ]


# --- POST /v1/me/transfers with recipient_currency --------------------------------------------------------


@pytest.mark.parametrize(
    ("recipient_currency", "recipient_amount_minor", "rate"),
    [("EUR", 9_200, "0.9200000000"), ("JPY", 15_150, "151.5000000000")],
    ids=["usd_to_eur", "usd_to_jpy"],
)
def test_post_transfer_with_a_recipient_currency_returns_201_with_the_fx_keys_and_converts(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    recipient_currency: str,
    recipient_amount_minor: int,
    rate: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _seed_fx(session_factory)
    alice = _funded_alice(session_factory, clock, 15_000)
    bob = _sign_up(session_factory, clock, BOB)

    # Act
    response = _post(
        client,
        TRANSFERS,
        _token(private_pem, clock, alice),
        _transfer_body(recipient_currency=recipient_currency),
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "transfer_id": str(uuid.UUID(body["transfer_id"])),
        "state": "succeeded",
        "amount_minor": 10_000,
        "currency": "USD",
        "sender_available_minor": 5_000,
        "recipient_currency": recipient_currency,
        "recipient_amount_minor": recipient_amount_minor,
        "fx_rate_id": body["fx_rate_id"],
    }
    assert response.headers["Cache-Control"] == NO_STORE
    with session_factory() as session:
        used = session.execute(
            text("SELECT base_currency, quote_currency, rate FROM gateway.fx_rates WHERE id = :id"),
            {"id": body["fx_rate_id"]},
        ).one()
    assert (used.base_currency, used.quote_currency, used.rate) == ("USD", recipient_currency, Decimal(rate))
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]
    assert _wallets(session_factory, bob) == [
        (recipient_currency, recipient_amount_minor, 0),
        ("USD", 0, 0),
    ]


def test_post_transfer_cross_currency_replay_returns_the_same_201_body_without_a_second_debit(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _seed_fx(session_factory)
    alice = _funded_alice(session_factory, clock, 15_000)
    bob = _sign_up(session_factory, clock, BOB)
    token = _token(private_pem, clock, alice)
    first = _post(client, TRANSFERS, token, _transfer_body())

    # Act
    replay = _post(client, TRANSFERS, token, _transfer_body())

    # Assert
    assert (replay.status_code, replay.json()) == (201, first.json())
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]
    assert _wallets(session_factory, bob) == [("EUR", 9_200, 0), ("USD", 0, 0)]


def test_post_transfer_with_the_same_key_and_a_different_recipient_currency_returns_422_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _seed_fx(session_factory)
    alice = _funded_alice(session_factory, clock, 15_000)
    _sign_up(session_factory, clock, BOB)
    token = _token(private_pem, clock, alice)
    _post(client, TRANSFERS, token, _transfer_body(recipient_currency="EUR"))

    # Act
    response = _post(client, TRANSFERS, token, _transfer_body(recipient_currency="JPY"))

    # Assert
    assert (response.status_code, response.json()) == (422, KEY_REUSED)
    assert _wallets(session_factory, alice) == [("USD", 5_000, 0)]


def test_post_transfer_with_recipient_currency_equal_to_currency_is_the_plain_same_currency_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_alice(session_factory, clock, 15_000)
    bob = _sign_up(session_factory, clock, BOB)

    # Act
    response = _post(
        client,
        TRANSFERS,
        _token(private_pem, clock, alice),
        _transfer_body(amount_minor=2_500, recipient_currency="USD"),
    )

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "transfer_id": str(uuid.UUID(body["transfer_id"])),
        "state": "succeeded",
        "amount_minor": 2_500,
        "currency": "USD",
        "sender_available_minor": 12_500,
    }
    assert _wallets(session_factory, bob) == [("USD", 2_500, 0)]


def test_post_transfer_without_a_recipient_currency_is_unchanged(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_alice(session_factory, clock, 15_000)
    bob = _sign_up(session_factory, clock, BOB)
    body = {"recipient_email": BOB, "amount_minor": 2_500, "currency": "USD"}

    # Act
    response = _post(client, TRANSFERS, _token(private_pem, clock, alice), body)

    # Assert
    assert response.status_code == 201
    assert sorted(response.json()) == [
        "amount_minor",
        "currency",
        "sender_available_minor",
        "state",
        "transfer_id",
    ]
    assert _wallets(session_factory, bob) == [("USD", 2_500, 0)]


def test_post_transfer_with_an_unsupported_recipient_currency_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _funded_alice(session_factory, clock, 15_000)
    _sign_up(session_factory, clock, BOB)

    # Act
    response = _post(
        client, TRANSFERS, _token(private_pem, clock, alice), _transfer_body(recipient_currency="GBP")
    )

    # Assert
    assert response.status_code == 422
    assert _wallets(session_factory, alice) == [("USD", 15_000, 0)]


def test_post_transfer_cross_currency_above_the_balance_returns_409_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _seed_fx(session_factory)
    alice = _funded_alice(session_factory, clock, 9_999)
    bob = _sign_up(session_factory, clock, BOB)

    # Act
    response = _post(client, TRANSFERS, _token(private_pem, clock, alice), _transfer_body())

    # Assert
    assert (response.status_code, response.json()) == (
        409,
        {"error": "insufficient_funds", "available_minor": 9_999},
    )
    assert _wallets(session_factory, alice) == [("USD", 9_999, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]


def test_post_transfer_cross_currency_that_converts_to_zero_returns_422_fx_amount_too_small(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _seed_fx(session_factory)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 1_000, currency="JPY")

    # Act
    response = _post(
        client,
        TRANSFERS,
        _token(private_pem, clock, alice),
        _transfer_body(amount_minor=1, currency="JPY", recipient_currency="USD"),
    )

    # Assert
    assert (response.status_code, response.json()) == (422, {"error": "fx_amount_too_small"})
    assert _wallets(session_factory, alice) == [("JPY", 1_000, 0), ("USD", 0, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]


def test_post_transfer_cross_currency_without_a_rate_returns_422_fx_rate_unavailable(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    with session_factory.begin() as session:
        session.execute(text("DELETE FROM gateway.fx_rates"))
    alice = _funded_alice(session_factory, clock, 15_000)
    bob = _sign_up(session_factory, clock, BOB)

    # Act
    response = _post(client, TRANSFERS, _token(private_pem, clock, alice), _transfer_body())

    # Assert
    assert (response.status_code, response.json()) == (422, {"error": "fx_rate_unavailable"})
    assert _wallets(session_factory, alice) == [("USD", 15_000, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]
