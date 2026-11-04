"""HTTP tests for POST /v1/me/transfers, written from docs/specs/gateway-basic.md section B2 (Route)."""

import uuid
from datetime import timedelta

import pyotp
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.accounts.tokens import issue_merchant_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.service import list_wallets, top_up_wallet
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

TRANSFERS = "/v1/me/transfers"
SESSIONS = "/v1/sessions"
ENROL = "/v1/me/mfa/totp"
VERIFY = "/v1/me/mfa/totp/verify"
ALICE = "alice@example.com"
BOB = "bob@example.com"
PASSWORD = "route-test-password-7Q"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
STEP_SECONDS = 30
MAX_AMOUNT_MINOR = 10**12
MISSING_SESSION = {"detail": "missing session"}
INVALID_SESSION = {"detail": "invalid session"}
MFA_REQUIRED = {"detail": "mfa required"}
KEY_REQUIRED = {"detail": "Idempotency-Key header is required"}
KEY_INVALID = {"detail": "invalid Idempotency-Key header"}
BAD_KEYS = ["a b", "a\tb", "a\x01b", "caf\xe9", "ключ", "a\x7fb"]
BAD_KEY_IDS = ["space", "tab", "control_x01", "latin1_non_ascii", "cyrillic", "del_x7f"]
MAX_VISIBLE_ASCII_KEY = "!~" * 127 + "a"
LONG_EMAIL = "a" * 309 + "@example.com"


def _generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def private_pem() -> str:
    return _generate_private_pem()


@pytest.fixture(scope="module")
def other_private_pem() -> str:
    return _generate_private_pem()


def _client(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> TestClient:
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
        totp_encryption_key=TOTP_KEY,
    )
    return TestClient(create_app(deps))


def _sign_up(
    session_factory: sessionmaker[Session], clock: FixedClock, email: str, currency: str = "USD"
) -> uuid.UUID:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD, currency=currency)
        session.commit()
    return result.user_id


def _fund(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    user_id: uuid.UUID,
    currency: str,
    amount_minor: int,
) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, user_id, currency, amount_minor)


def _wallets(session_factory: sessionmaker[Session], user_id: uuid.UUID) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        return [(v.currency, v.available_minor, v.held_minor) for v in list_wallets(session, user_id)]


def _count(session_factory: sessionmaker[Session], query: str) -> int:
    with session_factory() as session:
        return int(str(session.execute(text(query)).scalar_one()))


def _transfer_count(session_factory: sessionmaker[Session]) -> int:
    return _count(session_factory, "SELECT count(*) FROM gateway.transfers")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(private_pem: str, clock: FixedClock, user_id: uuid.UUID, amr: tuple[str, ...] = ("pwd",)) -> str:
    return issue_session_token(private_pem, user_id, clock(), amr)


def _body(
    recipient_email: object = BOB, amount_minor: object = 100, currency: object = "USD"
) -> dict[str, object]:
    return {"recipient_email": recipient_email, "amount_minor": amount_minor, "currency": currency}


def _post(client: TestClient, token: str | None, body: object, key: str | None = "k-1") -> Response:
    headers = {} if token is None else _bearer(token)
    if key is not None:
        headers["Idempotency-Key"] = key
    response: Response = client.post(TRANSFERS, headers=headers, json=body)
    return response


def _code(secret: str, clock: FixedClock) -> str:
    return pyotp.TOTP(secret).at(int(clock().timestamp()))


def _enable_totp(client: TestClient, clock: FixedClock, email: str) -> None:
    """Enrols and confirms TOTP for `email`, then moves the clock two steps on."""
    login = client.post(SESSIONS, json={"email": email, "password": PASSWORD})
    token = str(login.json()["access_token"])
    enrolled = client.post(ENROL, headers=_bearer(token))
    secret = str(enrolled.json()["secret"])
    confirmed = client.post(VERIFY, headers=_bearer(token), json={"code": _code(secret, clock)})
    assert confirmed.status_code == 200
    clock.advance(timedelta(seconds=2 * STEP_SECONDS))


# --- success ----------------------------------------------------------------------------------------------


def test_post_transfer_returns_201_with_the_transfer_body_and_moves_the_money(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=2_500))

    # Assert
    body = response.json()
    assert response.status_code == 201
    assert body == {
        "transfer_id": str(uuid.UUID(body["transfer_id"])),
        "state": "succeeded",
        "amount_minor": 2_500,
        "currency": "USD",
        "sender_available_minor": 7_500,
    }
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _wallets(session_factory, bob) == [("USD", 2_500, 0)]


def test_post_transfer_201_response_text_never_contains_the_recipient_user_id(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=2_500))

    # Assert
    assert response.status_code == 201
    assert str(bob) not in response.text
    assert "recipient_user_id" not in response.text


def test_post_transfer_replay_returns_the_same_201_body_without_a_second_debit(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    token = _token(private_pem, clock, alice)
    first = _post(client, token, _body(amount_minor=2_500))

    # Act
    replay = _post(client, token, _body(amount_minor=2_500))

    # Assert
    assert (replay.status_code, replay.json()) == (201, first.json())
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]
    assert _transfer_count(session_factory) == 1


def test_post_transfer_with_the_same_key_and_a_different_body_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 10_000)
    token = _token(private_pem, clock, alice)
    _post(client, token, _body(amount_minor=2_500))

    # Act
    response = _post(client, token, _body(amount_minor=2_501))

    # Assert
    assert (response.status_code, response.json()) == (422, {"error": "idempotency_key_reused"})
    assert _wallets(session_factory, alice) == [("USD", 7_500, 0)]


def test_post_transfer_creates_the_recipient_wallet_in_a_new_currency(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "EUR", 5_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=1_200, currency="EUR"))

    # Assert
    assert response.status_code == 201
    assert _wallets(session_factory, bob) == [("EUR", 1_200, 0), ("USD", 0, 0)]


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "INR"])
def test_post_transfer_accepts_each_supported_currency(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, currency: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, currency, 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=1_000, currency=currency))

    # Assert
    assert (response.status_code, response.json()["currency"]) == (201, currency)
    assert response.json()["sender_available_minor"] == 0


@pytest.mark.parametrize("amount_minor", [1, MAX_AMOUNT_MINOR], ids=["one", "ten_to_the_twelve"])
def test_post_transfer_accepts_the_amount_bounds(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, amount_minor: int
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", MAX_AMOUNT_MINOR)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=amount_minor))

    # Assert
    assert (response.status_code, response.json()["amount_minor"]) == (201, amount_minor)
    assert _wallets(session_factory, bob) == [("USD", amount_minor, 0)]


# --- outcomes mapped through ------------------------------------------------------------------------------


def test_post_transfer_maps_insufficient_funds_to_409_with_the_available_balance(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=1_001))

    # Assert
    assert (response.status_code, response.json()) == (
        409,
        {"error": "insufficient_funds", "available_minor": 1_000},
    )


def test_post_transfer_from_a_sender_without_a_wallet_in_that_currency_returns_409_with_zero_available(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(amount_minor=100, currency="JPY"))

    # Assert
    assert (response.status_code, response.json()) == (
        409,
        {"error": "insufficient_funds", "available_minor": 0},
    )


def test_post_transfer_maps_an_unknown_recipient_to_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(recipient_email="nobody@example.com"))

    # Assert
    assert (response.status_code, response.json()) == (404, {"error": "recipient_not_found"})


def test_post_transfer_maps_a_transfer_to_oneself_to_422_even_when_the_email_case_differs(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(recipient_email="ALICE@Example.com"))

    # Assert
    assert (response.status_code, response.json()) == (422, {"error": "self_transfer"})
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]


# --- Cache-Control ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "expected_status"),
    [
        ("created", 201),
        ("recipient_not_found", 404),
        ("insufficient_funds", 409),
        ("self_transfer", 422),
        ("idempotency_key_reused", 422),
        ("validation_failed", 422),
        ("missing_key", 400),
        ("invalid_key", 400),
    ],
)
def test_post_transfer_forbids_caching_on_every_answer(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    scenario: str,
    expected_status: int,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    token = _token(private_pem, clock, alice)
    if scenario == "idempotency_key_reused":
        _post(client, token, _body(amount_minor=100))
    requests: dict[str, tuple[dict[str, object], str | None]] = {
        "created": (_body(amount_minor=100), "k-1"),
        "recipient_not_found": (_body(recipient_email="nobody@example.com"), "k-1"),
        "insufficient_funds": (_body(amount_minor=5_000), "k-1"),
        "self_transfer": (_body(recipient_email=ALICE), "k-1"),
        "idempotency_key_reused": (_body(amount_minor=101), "k-1"),
        "validation_failed": (_body(amount_minor=0), "k-1"),
        "missing_key": (_body(), None),
        "invalid_key": (_body(), "a b"),
    }
    body, key = requests[scenario]

    # Act
    response = _post(client, token, body, key)

    # Assert
    assert response.status_code == expected_status
    assert response.headers["Cache-Control"] == "no-store"


# --- session ----------------------------------------------------------------------------------------------


def test_post_transfer_without_a_bearer_returns_401_missing_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, None, _body())

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)
    assert _transfer_count(session_factory) == 0


@pytest.mark.parametrize(
    "kind", ["garbage", "malformed", "expired", "other_key", "merchant_token", "api_key"]
)
def test_post_transfer_with_anything_but_a_live_gateway_session_returns_401_invalid_session(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    other_private_pem: str,
    kind: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    tokens = {
        "garbage": "not-a-jwt",
        "malformed": "x.y.z",
        "expired": issue_session_token(private_pem, alice, clock() - timedelta(hours=1), ("pwd",)),
        "other_key": issue_session_token(other_private_pem, alice, clock(), ("pwd",)),
        "merchant_token": issue_merchant_token(private_pem, uuid.uuid4(), clock()),
        "api_key": "sk_test_abcdefghijklmnopqrstuvwxyz0123456789",
    }

    # Act
    response = _post(client, tokens[kind], _body())

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_SESSION)
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _transfer_count(session_factory) == 0


def test_post_transfer_requires_an_otp_session_once_totp_is_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    _enable_totp(client, clock, ALICE)

    # Act
    response = _post(client, _token(private_pem, clock, alice, ("pwd",)), _body())

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _transfer_count(session_factory) == 0


def test_post_transfer_accepts_an_otp_session_once_totp_is_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    _enable_totp(client, clock, ALICE)

    # Act
    response = _post(client, _token(private_pem, clock, alice, ("pwd", "otp")), _body(amount_minor=400))

    # Assert
    assert response.status_code == 201
    assert _wallets(session_factory, alice) == [("USD", 600, 0)]
    assert _wallets(session_factory, bob) == [("USD", 400, 0)]


def test_post_transfer_accepts_a_password_only_session_for_a_user_without_totp(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice, ("pwd",)), _body(amount_minor=400))

    # Assert
    assert response.status_code == 201


# --- Idempotency-Key header -------------------------------------------------------------------------------


def test_post_transfer_without_an_idempotency_key_returns_400_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(), key=None)

    # Assert
    assert (response.status_code, response.json()) == (400, KEY_REQUIRED)
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _transfer_count(session_factory) == 0


def test_post_transfer_with_an_empty_idempotency_key_returns_400_required(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(), key="")

    # Assert
    assert (response.status_code, response.json()) == (400, KEY_REQUIRED)
    assert _transfer_count(session_factory) == 0


@pytest.mark.parametrize("key", BAD_KEYS, ids=BAD_KEY_IDS)
def test_post_transfer_with_a_non_visible_ascii_idempotency_key_returns_400_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, key: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)
    token = _token(private_pem, clock, alice)
    raw_headers = [
        (b"authorization", f"Bearer {token}".encode("ascii")),
        (b"idempotency-key", key.encode("utf-8")),
    ]

    # Act
    response = client.post(TRANSFERS, headers=raw_headers, json=_body())

    # Assert
    assert (response.status_code, response.json()) == (400, KEY_INVALID)
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _transfer_count(session_factory) == 0


def test_post_transfer_accepts_a_255_char_visible_ascii_idempotency_key(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(), key=MAX_VISIBLE_ASCII_KEY)

    # Assert
    assert len(MAX_VISIBLE_ASCII_KEY) == 255
    assert response.status_code == 201


# --- body validation --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {**_body(), "note": "lunch"},
        _body(amount_minor=True),
        _body(amount_minor=100.0),
        _body(amount_minor=100.5),
        _body(amount_minor="100"),
        _body(amount_minor=None),
        _body(amount_minor=0),
        _body(amount_minor=-1),
        _body(amount_minor=MAX_AMOUNT_MINOR + 1),
        _body(currency="GBP"),
        _body(currency="usd"),
        _body(currency="USDX"),
        _body(currency=None),
        _body(recipient_email=""),
        _body(recipient_email=LONG_EMAIL),
        _body(recipient_email=None),
        _body(recipient_email=123),
        {"amount_minor": 100, "currency": "USD"},
        {"recipient_email": BOB, "currency": "USD"},
        {"recipient_email": BOB, "amount_minor": 100},
        {},
        [],
    ],
    ids=[
        "extra_field",
        "amount_bool",
        "amount_float_whole",
        "amount_float_fraction",
        "amount_string",
        "amount_null",
        "amount_zero",
        "amount_negative",
        "amount_above_max",
        "currency_unsupported",
        "currency_lowercase",
        "currency_too_long",
        "currency_null",
        "recipient_empty",
        "recipient_above_320_chars",
        "recipient_null",
        "recipient_number",
        "missing_recipient",
        "missing_amount",
        "missing_currency",
        "empty_object",
        "array_body",
    ],
)
def test_post_transfer_with_an_invalid_body_returns_422_validation_failed_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, body: object
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), body)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _transfer_count(session_factory) == 0


def test_post_transfer_without_a_body_returns_422_validation_failed(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    headers = {**_bearer(_token(private_pem, clock, alice)), "Idempotency-Key": "k-1"}

    # Act
    response = client.post(TRANSFERS, headers=headers)

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"


def test_post_transfer_with_a_nul_character_in_the_recipient_email_returns_422_and_moves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, "USD", 1_000)

    # Act
    response = _post(client, _token(private_pem, clock, alice), _body(recipient_email="a\u0000@example.com"))

    # Assert
    assert response.status_code == 422
    assert response.json()["error"] == "validation_failed"
    assert response.headers["Cache-Control"] == "no-store"
    assert _wallets(session_factory, alice) == [("USD", 1_000, 0)]
    assert _wallets(session_factory, bob) == [("USD", 0, 0)]
    assert _transfer_count(session_factory) == 0
