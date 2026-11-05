"""RED tests for the consumer payment routes (B13.1), from docs/specs/disputes-notifications.md section 2:
POST /v1/me/payments, GET /v1/me/payments and POST /v1/me/payments/{id}/refund-requests.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import open_dispute, resolve_dispute, submit_evidence
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.refunds import refund_payment
from gateway.payments.schemas import ChargeRequest
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
PASSWORD = "consumer-payments-password-7Q"
CHARGE_RESPONSE_KEYS = {"payment_id", "state", "amount_minor", "currency", "method", "card", "decline_code"}
CONSUMER_PAYMENT_KEYS = {
    "payment_id",
    "merchant_id",
    "state",
    "amount_minor",
    "currency",
    "card",
    "decline_code",
    "refunded_minor",
    "disputed_minor",
    "chargeback_minor",
    "chargeback_flag",
    "created_at",
}
NO_STORE = "no-store"


@dataclass(frozen=True)
class Env:
    deps: ChargeDeps
    client: TestClient
    private_pem: str
    session_factory: sessionmaker[Session]
    clock: FixedClock
    merchant_id: uuid.UUID
    merchant_auth: dict[str, str]


@pytest.fixture(scope="module")
def private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _env(session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str) -> Env:
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
    app = create_app(
        GatewayAppDeps(charge=deps, api_key_pepper=TEST_API_KEY_PEPPER, jwt_private_key_pem=private_pem)
    )
    return Env(
        deps,
        TestClient(app),
        private_pem,
        session_factory,
        clock,
        credentials.merchant_id,
        {"Authorization": f"Bearer {credentials.api_key}"},
    )


def _add_merchant(env: Env, name: str) -> uuid.UUID:
    with env.session_factory() as session:
        credentials = create_merchant(session, env.clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return credentials.merchant_id


def _add_user(env: Env, email: str) -> uuid.UUID:
    with env.session_factory() as session:
        result = sign_up(session, env.clock, email, PASSWORD)
        session.commit()
    return result.user_id


def _user_auth(env: Env, user_id: uuid.UUID) -> dict[str, str]:
    """A session token issued at the clock's current time (the clock advances inside tests)."""
    token = issue_session_token(env.private_pem, user_id, env.clock(), ("pwd",))
    return {"Authorization": f"Bearer {token}"}


def _card(pan: str) -> dict[str, object]:
    return {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"}


def _pay_body(
    env: Env, pan: str = APPROVE_PAN, amount_minor: int = 1500, merchant_id: uuid.UUID | None = None
) -> dict[str, object]:
    return {
        "merchant_id": str(merchant_id or env.merchant_id),
        "amount_minor": amount_minor,
        "currency": "USD",
        "card": _card(pan),
    }


def _pay(
    env: Env,
    user_id: uuid.UUID,
    body: dict[str, object] | None = None,
    key: str | None = None,
    send_key: bool = True,
) -> Response:
    headers = _user_auth(env, user_id)
    if send_key:
        headers["Idempotency-Key"] = key or uuid.uuid4().hex
    response: Response = env.client.post(
        "/v1/me/payments", json=_pay_body(env) if body is None else body, headers=headers
    )
    return response


def _paid(
    env: Env,
    user_id: uuid.UUID,
    pan: str = APPROVE_PAN,
    amount_minor: int = 1500,
    merchant_id: uuid.UUID | None = None,
) -> uuid.UUID:
    response = _pay(env, user_id, _pay_body(env, pan, amount_minor, merchant_id))
    assert response.status_code == 201
    return uuid.UUID(str(response.json()["payment_id"]))


def _merchant_payment(
    env: Env, merchant_id: uuid.UUID | None = None, key: str | None = None, amount_minor: int = 1500
) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {"amount_minor": amount_minor, "currency": "USD", "method": "card", "card": _card(APPROVE_PAN)}
    )
    outcome = charge_card(env.deps, merchant_id or env.merchant_id, key or uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _list(env: Env, user_id: uuid.UUID, **params: str | int) -> Response:
    response: Response = env.client.get(
        "/v1/me/payments", params=dict(params), headers=_user_auth(env, user_id)
    )
    return response


def _request_refund(
    env: Env, user_id: uuid.UUID, payment_id: uuid.UUID, body: dict[str, object] | None = None
) -> Response:
    response: Response = env.client.post(
        f"/v1/me/payments/{payment_id}/refund-requests",
        json={} if body is None else body,
        headers=_user_auth(env, user_id),
    )
    return response


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _count(session_factory: sessionmaker[Session], table: str) -> int:
    assert table in {"payments", "journal_entries", "refunds"}
    query = f"SELECT count(*) AS n FROM gateway.{table}"  # noqa: S608
    return int(str(_rows(session_factory, query)[0]["n"]))


def _payment_row(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> dict[str, object]:
    return _rows(
        session_factory,
        "SELECT merchant_id, payer_user_id, state, refunded_minor, chargeback_flag "
        "FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]


def _merchant_notices(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, kind: str
) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT kind, payment_id, recipient_type, recipient_id FROM gateway.notifications "
        "WHERE recipient_type = 'merchant' AND recipient_id = :rid AND kind = :kind ORDER BY created_at, id",
        rid=merchant_id,
        kind=kind,
    )


def _iso(moment: datetime) -> str:
    return moment.isoformat()


# --- POST /v1/me/payments ----------------------------------------------------------------------------------


def test_paying_a_merchant_returns_201_with_the_charge_body_and_records_the_payer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _pay(env, user_id)

    # Assert
    body = response.json()
    row = _payment_row(session_factory, uuid.UUID(body["payment_id"]))
    assert response.status_code == 201
    assert set(body) == CHARGE_RESPONSE_KEYS
    assert (body["state"], body["amount_minor"], body["currency"], body["decline_code"]) == (
        "succeeded",
        1500,
        "USD",
        None,
    )
    assert body["card"] == {"bin": "424242", "last4": "4242"}
    assert (row["payer_user_id"], row["merchant_id"], row["state"]) == (user_id, env.merchant_id, "succeeded")


def test_paying_with_a_declined_card_returns_the_declined_charge_body_with_the_payer_set(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _pay(env, user_id, _pay_body(env, pan=DECLINED_PAN))

    # Assert
    body = response.json()
    row = _payment_row(session_factory, uuid.UUID(body["payment_id"]))
    assert response.status_code == 201
    assert (body["state"], body["decline_code"]) == ("declined", "card_declined")
    assert row["payer_user_id"] == user_id


def test_paying_twice_with_the_same_key_replays_the_same_body_and_creates_one_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    first = _pay(env, user_id, key="pay-once")

    # Act
    second = _pay(env, user_id, key="pay-once")

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json() == first.json()
    assert _count(session_factory, "payments") == 1


def test_reusing_a_key_for_a_different_amount_returns_422_idempotency_key_reused(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _pay(env, user_id, _pay_body(env, amount_minor=1500), key="same-key")

    # Act
    response = _pay(env, user_id, _pay_body(env, amount_minor=1600), key="same-key")

    # Assert
    assert response.status_code == 422
    assert response.json() == {"error": "idempotency_key_reused"}
    assert _count(session_factory, "payments") == 1


def test_the_users_key_is_stored_namespaced_and_does_not_collide_with_the_merchants_own_key(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    merchant_payment_id = _merchant_payment(env, key="shared-key")

    # Act
    response = _pay(env, user_id, key="shared-key")

    # Assert
    stored = _rows(session_factory, "SELECT merchant_id, key FROM gateway.idempotency_keys ORDER BY key")
    assert response.status_code == 201
    assert uuid.UUID(response.json()["payment_id"]) != merchant_payment_id
    assert _count(session_factory, "payments") == 2
    assert [(row["merchant_id"], row["key"]) for row in stored] == [
        (env.merchant_id, "shared-key"),
        (env.merchant_id, f"user:{user_id}:shared-key"),
    ]


def test_two_users_may_use_the_same_key_against_the_same_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _add_user(env, "alice@example.com")
    bob = _add_user(env, "bob@example.com")
    first = _pay(env, alice, key="their-key")

    # Act
    second = _pay(env, bob, key="their-key")

    # Assert
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["payment_id"] != second.json()["payment_id"]
    assert _payment_row(session_factory, uuid.UUID(second.json()["payment_id"]))["payer_user_id"] == bob


def test_paying_without_an_idempotency_key_returns_400_and_creates_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _pay(env, user_id, send_key=False)

    # Assert
    assert response.status_code == 400
    assert _count(session_factory, "payments") == 0


@pytest.mark.parametrize(
    "key", ["has space", "tab\tkey", "x" * 201, "ключ"], ids=["space", "control", "too-long", "non-ascii"]
)
def test_paying_with_an_invalid_idempotency_key_returns_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, key: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _pay(env, user_id, key=key)

    # Assert
    assert response.status_code == 400
    assert _count(session_factory, "payments") == 0


def test_paying_with_a_200_character_key_is_accepted(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _pay(env, user_id, key="k" * 200)

    # Assert
    assert response.status_code == 201


def test_paying_an_unknown_merchant_returns_404_merchant_not_found(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _pay(env, user_id, _pay_body(env, merchant_id=uuid.uuid4()))

    # Assert
    assert response.status_code == 404
    assert response.json() == {"error": "merchant_not_found"}
    assert _count(session_factory, "payments") == 0


def test_every_response_of_paying_a_merchant_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    created = _pay(env, user_id)
    unknown_merchant = _pay(env, user_id, _pay_body(env, merchant_id=uuid.uuid4()))
    missing_key = _pay(env, user_id, send_key=False)

    # Assert
    assert [r.status_code for r in (created, unknown_merchant, missing_key)] == [201, 404, 400]
    assert [r.headers["Cache-Control"] for r in (created, unknown_merchant, missing_key)] == [NO_STORE] * 3


def test_paying_without_a_valid_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    anonymous = env.client.post("/v1/me/payments", json=_pay_body(env), headers={"Idempotency-Key": "k1"})
    with_api_key = env.client.post(
        "/v1/me/payments", json=_pay_body(env), headers={**env.merchant_auth, "Idempotency-Key": "k2"}
    )

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)
    assert _count(session_factory, "payments") == 0


@pytest.mark.parametrize(
    "mutation",
    [
        {"amount_minor": 0},
        {"amount_minor": -5},
        {"currency": "GBP"},
        {"merchant_id": "not-a-uuid"},
        {"card": _card("4111111111111111")},
        {"unexpected": "field"},
    ],
    ids=["zero-amount", "negative-amount", "bad-currency", "bad-merchant-id", "non-test-pan", "extra-field"],
)
def test_paying_with_an_invalid_body_returns_422_and_creates_nothing(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    mutation: dict[str, object],
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    body = {**_pay_body(env), **mutation}

    # Act
    response = _pay(env, user_id, body)

    # Assert
    assert response.status_code == 422
    assert _count(session_factory, "payments") == 0


# --- GET /v1/me/payments -----------------------------------------------------------------------------------


def test_my_payments_returns_the_consumer_payment_with_exactly_the_specified_fields(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _list(env, user_id)

    # Assert
    body = response.json()
    item = body["items"][0]
    assert response.status_code == 200
    assert set(body) == {"items", "next_cursor"}
    assert len(body["items"]) == 1
    assert set(item) == CONSUMER_PAYMENT_KEYS
    assert datetime.fromisoformat(item.pop("created_at")) == clock()
    assert item == {
        "payment_id": str(payment_id),
        "merchant_id": str(env.merchant_id),
        "state": "succeeded",
        "amount_minor": 1500,
        "currency": "USD",
        "card": {"bin": "424242", "last4": "4242"},
        "decline_code": None,
        "refunded_minor": 0,
        "disputed_minor": 0,
        "chargeback_minor": 0,
        "chargeback_flag": False,
    }
    assert body["next_cursor"] is None


def test_my_payments_lists_only_payments_the_user_paid_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _add_user(env, "alice@example.com")
    bob = _add_user(env, "bob@example.com")
    first = _paid(env, alice)
    clock.advance(timedelta(seconds=1))
    _paid(env, bob)
    clock.advance(timedelta(seconds=1))
    _merchant_payment(env)
    clock.advance(timedelta(seconds=1))
    second = _paid(env, alice)

    # Act
    response = _list(env, alice)

    # Assert
    assert response.status_code == 200
    assert [item["payment_id"] for item in response.json()["items"]] == [str(second), str(first)]


def test_my_payments_is_empty_for_a_user_who_paid_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payer = _add_user(env, "payer@example.com")
    bystander = _add_user(env, "bystander@example.com")
    _paid(env, payer)

    # Act
    response = _list(env, bystander)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_my_payments_shows_a_declined_payment_with_its_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id, pan=DECLINED_PAN)

    # Act
    response = _list(env, user_id)

    # Assert
    item = response.json()["items"][0]
    assert (item["payment_id"], item["state"], item["decline_code"]) == (
        str(payment_id),
        "declined",
        "card_declined",
    )
    assert item["card"] == {"bin": "400000", "last4": "0002"}


def test_my_payments_filters_by_state(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    succeeded = _paid(env, user_id)
    clock.advance(timedelta(seconds=1))
    declined = _paid(env, user_id, pan=DECLINED_PAN)

    # Act
    only_succeeded = _list(env, user_id, state="succeeded")
    only_declined = _list(env, user_id, state="declined")

    # Assert
    assert [item["payment_id"] for item in only_succeeded.json()["items"]] == [str(succeeded)]
    assert [item["payment_id"] for item in only_declined.json()["items"]] == [str(declined)]


def test_my_payments_date_range_is_half_open(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    t0 = clock()
    first = _paid(env, user_id)
    clock.advance(timedelta(hours=1))
    t1 = clock()
    second = _paid(env, user_id)
    clock.advance(timedelta(hours=1))
    t2 = clock()
    third = _paid(env, user_id)

    # Act
    from_t1 = _list(env, user_id, created_from=_iso(t1))
    before_t2 = _list(env, user_id, created_to=_iso(t2))
    window = _list(env, user_id, created_from=_iso(t1), created_to=_iso(t2))

    # Assert
    assert t0 < t1 < t2
    assert [item["payment_id"] for item in from_t1.json()["items"]] == [str(third), str(second)]
    assert [item["payment_id"] for item in before_t2.json()["items"]] == [str(second), str(first)]
    assert [item["payment_id"] for item in window.json()["items"]] == [str(second)]


@pytest.mark.parametrize(
    "params",
    [
        {"created_from": "2026-11-03T12:00:00"},
        {"created_to": "2026-11-03"},
        {"created_from": "yesterday"},
        {"created_from": "2026-11-03T12:00:00+00:00", "created_to": "2026-11-03T12:00:00+00:00"},
        {"created_from": "2026-11-04T12:00:00+00:00", "created_to": "2026-11-03T12:00:00+00:00"},
    ],
    ids=["naive-from", "date-only-to", "garbage", "empty-range", "inverted-range"],
)
def test_my_payments_rejects_a_bad_date_filter_with_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, params: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _list(env, user_id, **params)

    # Assert
    assert response.status_code == 400


def test_my_payments_pages_with_a_cursor_without_gaps_or_repeats(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    paid: list[uuid.UUID] = []
    for _ in range(3):
        paid.append(_paid(env, user_id))
        clock.advance(timedelta(seconds=1))

    # Act
    page_one = _list(env, user_id, limit=2).json()
    page_two = _list(env, user_id, limit=2, cursor=page_one["next_cursor"]).json()

    # Assert
    assert [item["payment_id"] for item in page_one["items"]] == [str(paid[2]), str(paid[1])]
    assert page_one["next_cursor"] is not None
    assert [item["payment_id"] for item in page_two["items"]] == [str(paid[0])]
    assert page_two["next_cursor"] is None


def test_my_payments_rejects_a_bad_cursor_with_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _list(env, user_id, cursor="not-a-cursor")

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


@pytest.mark.parametrize("limit", [0, 101])
def test_my_payments_rejects_a_limit_outside_1_to_100_with_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, limit: int
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _list(env, user_id, limit=limit)

    # Assert
    assert response.status_code == 422


def test_my_payments_without_a_valid_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    anonymous = env.client.get("/v1/me/payments")
    with_api_key = env.client.get("/v1/me/payments", headers=env.merchant_auth)

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)


def test_my_payments_reports_refunded_and_disputed_amounts(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id, amount_minor=2000)
    refund_payment(session_factory, clock, env.merchant_id, payment_id, "refund-1", 500)
    open_dispute(session_factory, clock, payment_id, "fraudulent", 700)

    # Act
    item = _list(env, user_id).json()["items"][0]

    # Assert
    assert (item["refunded_minor"], item["disputed_minor"], item["chargeback_minor"]) == (500, 700, 0)
    assert item["chargeback_flag"] is False


def test_my_payments_reports_the_chargeback_flag_once_a_dispute_is_lost(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    opened = open_dispute(session_factory, clock, payment_id, "fraudulent")
    submit_evidence(session_factory, clock, env.merchant_id, opened.dispute_id, "receipt attached")
    resolve_dispute(session_factory, clock, opened.dispute_id, "lost", "ops_lead:test")

    # Act
    item = _list(env, user_id).json()["items"][0]

    # Assert
    assert item["chargeback_flag"] is True
    assert (item["disputed_minor"], item["chargeback_minor"]) == (0, 1500)


# --- POST /v1/me/payments/{payment_id}/refund-requests -----------------------------------------------------


def test_a_refund_request_without_an_amount_asks_for_the_full_refundable_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _request_refund(env, user_id, payment_id)

    # Assert
    assert response.status_code == 201
    assert response.json() == {"payment_id": str(payment_id), "requested_minor": 1500, "state": "requested"}


def test_a_refund_request_with_an_amount_and_a_note_echoes_the_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _request_refund(env, user_id, payment_id, {"amount_minor": 600, "note": "Item arrived broken"})

    # Assert
    assert response.status_code == 201
    assert response.json() == {"payment_id": str(payment_id), "requested_minor": 600, "state": "requested"}


def test_a_refund_request_after_a_partial_refund_asks_for_what_is_left(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    refund_payment(session_factory, clock, env.merchant_id, payment_id, "refund-1", 500)

    # Act
    response = _request_refund(env, user_id, payment_id)

    # Assert
    assert response.status_code == 201
    assert response.json()["requested_minor"] == 1000


def test_a_refund_request_moves_no_money(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    journals_before = _count(session_factory, "journal_entries")

    # Act
    _request_refund(env, user_id, payment_id)

    # Assert
    assert _payment_row(session_factory, payment_id)["refunded_minor"] == 0
    assert _count(session_factory, "refunds") == 0
    assert _count(session_factory, "journal_entries") == journals_before


def test_a_refund_request_creates_a_refund_requested_notification_for_the_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    _request_refund(env, user_id, payment_id)

    # Assert
    assert _merchant_notices(session_factory, env.merchant_id, "refund_requested") == [
        {
            "kind": "refund_requested",
            "payment_id": payment_id,
            "recipient_type": "merchant",
            "recipient_id": env.merchant_id,
        }
    ]


def test_a_refund_request_does_not_notify_another_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    other_id = _add_merchant(env, "Other")
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    _request_refund(env, user_id, payment_id)

    # Assert
    assert _merchant_notices(session_factory, other_id, "refund_requested") == []


def test_a_refund_request_for_another_users_payment_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    owner = _add_user(env, "owner@example.com")
    stranger = _add_user(env, "stranger@example.com")
    payment_id = _paid(env, owner)

    # Act
    response = _request_refund(env, stranger, payment_id)

    # Assert
    assert response.status_code == 404
    assert response.json()["error"] == "payment_not_found"
    assert _merchant_notices(session_factory, env.merchant_id, "refund_requested") == []


def test_a_refund_request_for_a_merchant_only_or_unknown_payment_returns_the_same_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    merchant_only = _merchant_payment(env)

    # Act
    foreign = _request_refund(env, user_id, merchant_only)
    unknown = _request_refund(env, user_id, uuid.uuid4())

    # Assert
    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert foreign.json() == unknown.json()
    assert foreign.json()["error"] == "payment_not_found"


def test_a_refund_request_for_a_payment_that_did_not_succeed_returns_409_not_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id, pan=DECLINED_PAN)

    # Act
    response = _request_refund(env, user_id, payment_id)

    # Assert
    assert response.status_code == 409
    assert response.json()["error"] == "payment_not_refundable"


def test_a_refund_request_for_a_fully_refunded_payment_returns_409_not_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    refund_payment(session_factory, clock, env.merchant_id, payment_id, "refund-all", None)

    # Act
    response = _request_refund(env, user_id, payment_id)

    # Assert
    assert response.status_code == 409
    assert response.json()["error"] == "payment_not_refundable"


def test_a_refund_request_while_a_dispute_reserves_the_whole_payment_returns_409_not_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    open_dispute(session_factory, clock, payment_id, "fraudulent")

    # Act
    response = _request_refund(env, user_id, payment_id)

    # Assert
    assert response.status_code == 409
    assert response.json()["error"] == "payment_not_refundable"


def test_a_refund_request_above_the_refundable_amount_returns_409_and_can_be_retried(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    too_much = _request_refund(env, user_id, payment_id, {"amount_minor": 1501})
    retried = _request_refund(env, user_id, payment_id, {"amount_minor": 1500})

    # Assert
    assert too_much.status_code == 409
    assert too_much.json()["error"] == "refund_exceeds_refundable"
    assert retried.status_code == 201
    assert len(_merchant_notices(session_factory, env.merchant_id, "refund_requested")) == 1


@pytest.mark.parametrize("second_body", [{}, {"amount_minor": 100}], ids=["same-request", "different-amount"])
def test_a_second_refund_request_for_the_same_payment_returns_409_already_requested(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    second_body: dict[str, object],
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    first = _request_refund(env, user_id, payment_id)
    assert first.status_code == 201

    # Act
    second = _request_refund(env, user_id, payment_id, second_body)

    # Assert
    assert second.status_code == 409
    assert second.json()["error"] == "refund_already_requested"
    assert len(_merchant_notices(session_factory, env.merchant_id, "refund_requested")) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"amount_minor": 0},
        {"amount_minor": -1},
        {"amount_minor": "100"},
        {"note": "x" * 281},
        {"note": "bad\x00note"},
        {"note": "zero​width"},
    ],
    ids=["zero", "negative", "string-amount", "note-too-long", "nul-in-note", "zero-width-in-note"],
)
def test_a_refund_request_with_an_invalid_body_returns_422_and_notifies_nobody(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    body: dict[str, object],
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _request_refund(env, user_id, payment_id, body)

    # Assert
    assert response.status_code == 422
    assert _merchant_notices(session_factory, env.merchant_id, "refund_requested") == []


def test_a_refund_request_accepts_a_note_of_exactly_280_characters(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _request_refund(env, user_id, payment_id, {"note": "n" * 280})

    # Assert
    assert response.status_code == 201


def test_a_refund_request_without_a_valid_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    url = f"/v1/me/payments/{payment_id}/refund-requests"

    # Act
    anonymous = env.client.post(url, json={})
    with_api_key = env.client.post(url, json={}, headers=env.merchant_auth)

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)
    assert _merchant_notices(session_factory, env.merchant_id, "refund_requested") == []
