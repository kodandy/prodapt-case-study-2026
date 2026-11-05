"""RED tests for the dispute routes (B3.3) and their notifications, from docs/specs/disputes-notifications.md
sections 3 and 4: consumer open, dispute lists, simulated-network open/resolve, ops resolution, the
chargeback flag, the refund/dispute/chargeback total, and the dispute_* notifications.
"""

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal

import pytest
from contracts.events import NOTIFICATIONS_TOPIC
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import open_dispute, submit_evidence
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest, DisputeView
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
PASSWORD = "dispute-route-password-7Q"
NETWORK_TOKEN = "test-network-token-not-a-secret"
OPS_TOKEN = "test-ops-admin-token-not-a-secret"
NETWORK_HEADERS = {"X-Network-Token": NETWORK_TOKEN}
OPS_HEADERS = {"X-Ops-Admin-Token": OPS_TOKEN}
DISPUTE_VIEW_KEYS = {
    "dispute_id",
    "payment_id",
    "state",
    "amount_minor",
    "currency",
    "reason",
    "chargeback_fee_minor",
    "created_at",
    "updated_at",
}
PAGE_KEYS = {"items", "next_cursor"}
INVALID_TRANSITION = {"detail": "invalid dispute transition"}
Via = Literal["network", "ops"]


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


def _env(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    network_token: str | None = NETWORK_TOKEN,
    ops_admin_token: str | None = OPS_TOKEN,
) -> Env:
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
        GatewayAppDeps(
            charge=deps,
            api_key_pepper=TEST_API_KEY_PEPPER,
            jwt_private_key_pem=private_pem,
            network_token=network_token,
            ops_admin_token=ops_admin_token,
        )
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


def _add_merchant(env: Env, name: str) -> tuple[uuid.UUID, dict[str, str]]:
    with env.session_factory() as session:
        credentials = create_merchant(session, env.clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return credentials.merchant_id, {"Authorization": f"Bearer {credentials.api_key}"}


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


def _pay(
    env: Env,
    user_id: uuid.UUID,
    pan: str = APPROVE_PAN,
    amount_minor: int = 1500,
    merchant_id: uuid.UUID | None = None,
) -> Response:
    response: Response = env.client.post(
        "/v1/me/payments",
        json={
            "merchant_id": str(merchant_id or env.merchant_id),
            "amount_minor": amount_minor,
            "currency": "USD",
            "card": _card(pan),
        },
        headers={**_user_auth(env, user_id), "Idempotency-Key": uuid.uuid4().hex},
    )
    return response


def _paid(
    env: Env, user_id: uuid.UUID, amount_minor: int = 1500, merchant_id: uuid.UUID | None = None
) -> uuid.UUID:
    response = _pay(env, user_id, amount_minor=amount_minor, merchant_id=merchant_id)
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    return uuid.UUID(str(response.json()["payment_id"]))


def _merchant_payment(
    env: Env, merchant_id: uuid.UUID | None = None, pan: str = APPROVE_PAN, amount_minor: int = 1500
) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {"amount_minor": amount_minor, "currency": "USD", "method": "card", "card": _card(pan)}
    )
    outcome = charge_card(env.deps, merchant_id or env.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _consumer_open(
    env: Env, user_id: uuid.UUID, payment_id: uuid.UUID, body: dict[str, object] | None = None
) -> Response:
    response: Response = env.client.post(
        f"/v1/me/payments/{payment_id}/disputes",
        json={"reason": "fraudulent"} if body is None else body,
        headers=_user_auth(env, user_id),
    )
    return response


def _consumer_opened(env: Env, user_id: uuid.UUID, payment_id: uuid.UUID) -> uuid.UUID:
    response = _consumer_open(env, user_id, payment_id)
    assert response.status_code == 201
    return uuid.UUID(str(response.json()["dispute_id"]))


def _network_open(
    env: Env,
    payment_id: uuid.UUID,
    reason: str = "fraudulent",
    amount_minor: int | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    body: dict[str, object] = {"payment_id": str(payment_id), "reason": reason}
    if amount_minor is not None:
        body["amount_minor"] = amount_minor
    response: Response = env.client.post(
        "/v1/network/disputes", json=body, headers=NETWORK_HEADERS if headers is None else headers
    )
    return response


def _network_opened(env: Env, payment_id: uuid.UUID) -> uuid.UUID:
    response = _network_open(env, payment_id)
    assert response.status_code == 201
    return uuid.UUID(str(response.json()["dispute_id"]))


def _evidence(env: Env, dispute_id: uuid.UUID, auth: dict[str, str] | None = None) -> None:
    response = env.client.post(
        f"/v1/disputes/{dispute_id}/evidence",
        json={"text": "receipt attached"},
        headers=env.merchant_auth if auth is None else auth,
    )
    assert response.status_code == 200


def _under_review(env: Env, payment_id: uuid.UUID) -> uuid.UUID:
    dispute_id = _network_opened(env, payment_id)
    _evidence(env, dispute_id)
    return dispute_id


def _resolve(
    env: Env, via: Via, dispute_id: uuid.UUID, outcome: str, headers: dict[str, str] | None = None
) -> Response:
    if via == "network":
        path, default = f"/v1/network/disputes/{dispute_id}/resolution", NETWORK_HEADERS
    else:
        path, default = f"/v1/ops/disputes/{dispute_id}/resolution", OPS_HEADERS
    response: Response = env.client.post(
        path, json={"outcome": outcome}, headers=default if headers is None else headers
    )
    return response


def _refund(env: Env, payment_id: uuid.UUID, amount_minor: int) -> Response:
    response: Response = env.client.post(
        f"/v1/payments/{payment_id}/refunds",
        json={"amount_minor": amount_minor},
        headers={**env.merchant_auth, "Idempotency-Key": uuid.uuid4().hex},
    )
    return response


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _totals(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> tuple[int, int, int]:
    row = _rows(
        session_factory,
        "SELECT refunded_minor, disputed_minor, chargeback_minor FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]
    return int(str(row["refunded_minor"])), int(str(row["disputed_minor"])), int(str(row["chargeback_minor"]))


def _chargeback_flag(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> bool:
    row = _rows(
        session_factory, "SELECT chargeback_flag FROM gateway.payments WHERE id = :id", id=payment_id
    )[0]
    return row["chargeback_flag"] is True


def _dispute_state(session_factory: sessionmaker[Session], dispute_id: uuid.UUID) -> str:
    row = _rows(session_factory, "SELECT state FROM gateway.disputes WHERE id = :id", id=dispute_id)[0]
    return str(row["state"])


def _dispute_count(session_factory: sessionmaker[Session]) -> int:
    return int(str(_rows(session_factory, "SELECT count(*) AS n FROM gateway.disputes")[0]["n"]))


def _journal_count(session_factory: sessionmaker[Session], ref_type: str) -> int:
    rows = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.journal_entries WHERE ref_type = :ref_type",
        ref_type=ref_type,
    )
    return int(str(rows[0]["n"]))


def _audit(session_factory: sessionmaker[Session], action: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action, target FROM gateway.audit_log "
        "WHERE action = :action ORDER BY id",
        action=action,
    )


def _notices(
    session_factory: sessionmaker[Session], recipient_type: str, recipient_id: uuid.UUID, kind: str
) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT kind, payment_id, recipient_type, recipient_id FROM gateway.notifications "
        "WHERE recipient_type = :rtype AND recipient_id = :rid AND kind = :kind ORDER BY created_at, id",
        rtype=recipient_type,
        rid=recipient_id,
        kind=kind,
    )


# --- POST /v1/me/payments/{payment_id}/disputes ---------------------------------------------------------


def test_consumer_opens_a_dispute_on_their_own_payment_and_gets_201_with_the_view(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _consumer_open(env, user_id, payment_id, {"reason": "product_not_received"})

    # Assert
    view = DisputeView.model_validate(response.json())
    assert response.status_code == 201
    assert set(response.json()) == DISPUTE_VIEW_KEYS
    assert (view.payment_id, view.state, view.amount_minor, view.currency, view.reason) == (
        payment_id,
        "opened",
        1500,
        "USD",
        "product_not_received",
    )
    assert _totals(session_factory, payment_id) == (0, 1500, 0)


def test_consumer_dispute_with_a_partial_amount_reserves_only_that_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _consumer_open(env, user_id, payment_id, {"reason": "fraudulent", "amount_minor": 700})

    # Assert
    assert response.status_code == 201
    assert response.json()["amount_minor"] == 700
    assert _totals(session_factory, payment_id) == (0, 700, 0)


def test_consumer_cannot_dispute_another_users_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    owner = _add_user(env, "owner@example.com")
    stranger = _add_user(env, "stranger@example.com")
    payment_id = _paid(env, owner)

    # Act
    response = _consumer_open(env, stranger, payment_id)

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "payment not found"}
    assert _dispute_count(session_factory) == 0
    assert _totals(session_factory, payment_id) == (0, 0, 0)


def test_consumer_cannot_dispute_a_merchant_only_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _merchant_payment(env)

    # Act
    response = _consumer_open(env, user_id, payment_id)

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "payment not found"}
    assert _dispute_count(session_factory) == 0


def test_consumer_dispute_on_an_unknown_payment_returns_the_same_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _consumer_open(env, user_id, uuid.uuid4())

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "payment not found"}


def test_consumer_dispute_on_a_payment_that_did_not_succeed_returns_409(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    declined = _pay(env, user_id, pan=DECLINED_PAN)
    assert declined.status_code == 201
    assert declined.json()["state"] == "declined"

    # Act
    response = _consumer_open(env, user_id, uuid.UUID(str(declined.json()["payment_id"])))

    # Assert
    assert response.status_code == 409
    assert response.json() == {"detail": "payment not disputable"}
    assert _dispute_count(session_factory) == 0


def test_consumer_dispute_while_one_is_open_returns_409_and_keeps_a_single_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    _consumer_opened(env, user_id, payment_id)

    # Act
    response = _consumer_open(env, user_id, payment_id)

    # Assert
    assert response.status_code == 409
    assert response.json() == {"detail": "dispute already open"}
    assert _dispute_count(session_factory) == 1
    assert _totals(session_factory, payment_id) == (0, 1500, 0)


def test_consumer_dispute_above_the_refundable_amount_returns_409(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _consumer_open(env, user_id, payment_id, {"reason": "fraudulent", "amount_minor": 1501})

    # Assert
    assert response.status_code == 409
    assert response.json() == {"detail": "dispute amount exceeds"}
    assert _totals(session_factory, payment_id) == (0, 0, 0)


@pytest.mark.parametrize(
    "reason",
    ["", "   ", "x" * 65, "line\nbreak", "zero​width"],
    ids=["empty", "blank", "too-long", "newline", "zero-width"],
)
def test_consumer_dispute_with_a_bad_reason_returns_422_and_reserves_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, reason: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    response = _consumer_open(env, user_id, payment_id, {"reason": reason})

    # Assert
    assert response.status_code == 422
    assert _dispute_count(session_factory) == 0
    assert _totals(session_factory, payment_id) == (0, 0, 0)


def test_consumer_dispute_with_a_non_uuid_payment_id_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = env.client.post(
        "/v1/me/payments/not-a-uuid/disputes", json={"reason": "fraudulent"}, headers=_user_auth(env, user_id)
    )

    # Assert
    assert response.status_code == 422


def test_consumer_dispute_without_a_valid_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    url = f"/v1/me/payments/{payment_id}/disputes"

    # Act
    anonymous = env.client.post(url, json={"reason": "fraudulent"})
    with_api_key = env.client.post(url, json={"reason": "fraudulent"}, headers=env.merchant_auth)

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)
    assert _dispute_count(session_factory) == 0


# --- GET /v1/me/disputes ---------------------------------------------------------------------------------


def test_my_disputes_lists_only_the_users_disputes_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _add_user(env, "alice@example.com")
    bob = _add_user(env, "bob@example.com")
    first = _consumer_opened(env, alice, _paid(env, alice))
    clock.advance(timedelta(seconds=1))
    _consumer_opened(env, bob, _paid(env, bob))
    clock.advance(timedelta(seconds=1))
    second = _consumer_opened(env, alice, _paid(env, alice))

    # Act
    response = env.client.get("/v1/me/disputes", headers=_user_auth(env, alice))

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == PAGE_KEYS
    assert [item["dispute_id"] for item in body["items"]] == [str(second), str(first)]
    assert all(set(item) == DISPUTE_VIEW_KEYS for item in body["items"])
    assert body["next_cursor"] is None


def test_my_disputes_includes_a_dispute_the_network_opened_on_the_users_payment(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    dispute_id = _network_opened(env, _paid(env, user_id))

    # Act
    response = env.client.get("/v1/me/disputes", headers=_user_auth(env, user_id))

    # Assert
    assert [item["dispute_id"] for item in response.json()["items"]] == [str(dispute_id)]


def test_my_disputes_is_empty_for_a_user_whose_payments_have_none(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payer = _add_user(env, "payer@example.com")
    bystander = _add_user(env, "bystander@example.com")
    _consumer_opened(env, payer, _paid(env, payer))

    # Act
    response = env.client.get("/v1/me/disputes", headers=_user_auth(env, bystander))

    # Assert
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_my_disputes_pages_with_a_cursor_without_gaps_or_repeats(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    opened: list[uuid.UUID] = []
    for _ in range(3):
        opened.append(_consumer_opened(env, user_id, _paid(env, user_id)))
        clock.advance(timedelta(seconds=1))
    headers = _user_auth(env, user_id)

    # Act
    page_one = env.client.get("/v1/me/disputes", params={"limit": 2}, headers=headers).json()
    page_two = env.client.get(
        "/v1/me/disputes", params={"limit": 2, "cursor": page_one["next_cursor"]}, headers=headers
    ).json()

    # Assert
    assert [item["dispute_id"] for item in page_one["items"]] == [str(opened[2]), str(opened[1])]
    assert page_one["next_cursor"] is not None
    assert [item["dispute_id"] for item in page_two["items"]] == [str(opened[0])]
    assert page_two["next_cursor"] is None


def test_my_disputes_rejects_a_bad_cursor_with_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = env.client.get(
        "/v1/me/disputes", params={"cursor": "not-a-cursor"}, headers=_user_auth(env, user_id)
    )

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


@pytest.mark.parametrize("limit", [0, 101])
def test_my_disputes_rejects_a_limit_outside_1_to_100_with_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, limit: int
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = env.client.get("/v1/me/disputes", params={"limit": limit}, headers=_user_auth(env, user_id))

    # Assert
    assert response.status_code == 422


def test_my_disputes_without_a_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    anonymous = env.client.get("/v1/me/disputes")
    with_api_key = env.client.get("/v1/me/disputes", headers=env.merchant_auth)

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)


# --- GET /v1/disputes ------------------------------------------------------------------------------------


def test_merchant_disputes_lists_only_the_callers_disputes_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    other_id, _ = _add_merchant(env, "Other")
    first = _network_opened(env, _merchant_payment(env))
    clock.advance(timedelta(seconds=1))
    _network_opened(env, _merchant_payment(env, other_id))
    clock.advance(timedelta(seconds=1))
    second = _network_opened(env, _merchant_payment(env))

    # Act
    response = env.client.get("/v1/disputes", headers=env.merchant_auth)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == PAGE_KEYS
    assert [item["dispute_id"] for item in body["items"]] == [str(second), str(first)]
    assert all(set(item) == DISPUTE_VIEW_KEYS for item in body["items"])
    assert body["next_cursor"] is None


@pytest.mark.parametrize(
    ("state", "expected_index"),
    [("opened", 0), ("under_review", 1)],
)
def test_merchant_disputes_filters_by_state(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    state: str,
    expected_index: int,
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    still_open = _network_opened(env, _merchant_payment(env))
    clock.advance(timedelta(seconds=1))
    reviewed = _under_review(env, _merchant_payment(env))
    expected = [still_open, reviewed][expected_index]

    # Act
    response = env.client.get("/v1/disputes", params={"state": state}, headers=env.merchant_auth)

    # Assert
    assert response.status_code == 200
    assert [item["dispute_id"] for item in response.json()["items"]] == [str(expected)]


def test_merchant_disputes_state_filter_with_no_match_returns_an_empty_page(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _network_opened(env, _merchant_payment(env))

    # Act
    response = env.client.get("/v1/disputes", params={"state": "won"}, headers=env.merchant_auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_merchant_disputes_rejects_an_unknown_state_with_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = env.client.get("/v1/disputes", params={"state": "bogus"}, headers=env.merchant_auth)

    # Assert
    assert response.status_code == 422


def test_merchant_disputes_pages_with_a_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    opened: list[uuid.UUID] = []
    for _ in range(3):
        opened.append(_network_opened(env, _merchant_payment(env)))
        clock.advance(timedelta(seconds=1))

    # Act
    page_one = env.client.get("/v1/disputes", params={"limit": 2}, headers=env.merchant_auth).json()
    page_two = env.client.get(
        "/v1/disputes", params={"limit": 2, "cursor": page_one["next_cursor"]}, headers=env.merchant_auth
    ).json()

    # Assert
    assert [item["dispute_id"] for item in page_one["items"]] == [str(opened[2]), str(opened[1])]
    assert [item["dispute_id"] for item in page_two["items"]] == [str(opened[0])]
    assert page_two["next_cursor"] is None


def test_merchant_disputes_rejects_a_bad_cursor_with_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = env.client.get("/v1/disputes", params={"cursor": "not-a-cursor"}, headers=env.merchant_auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


def test_merchant_disputes_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    anonymous = env.client.get("/v1/disputes")
    wrong_key = env.client.get("/v1/disputes", headers={"Authorization": "Bearer sk_test_x"})

    # Assert
    assert (anonymous.status_code, wrong_key.status_code) == (401, 401)


def test_the_merchant_api_key_still_cannot_open_or_resolve_a_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    reviewed = _under_review(env, _merchant_payment(env))

    # Act
    open_response = env.client.post(
        "/v1/disputes",
        json={"payment_id": str(payment_id), "reason": "fraudulent"},
        headers=env.merchant_auth,
    )
    resolve_response = env.client.post(
        f"/v1/disputes/{reviewed}/resolve", json={"outcome": "won"}, headers=env.merchant_auth
    )

    # Assert
    assert open_response.status_code == 405
    assert resolve_response.status_code == 404
    assert _dispute_state(session_factory, reviewed) == "under_review"


# --- network routes: auth --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Network-Token": "wrong-token"}, {"X-Network-Token": ""}, OPS_HEADERS],
    ids=["missing", "wrong", "empty", "ops-token-in-the-wrong-header"],
)
def test_network_open_with_a_missing_or_wrong_token_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, headers: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)

    # Act
    response = _network_open(env, payment_id, headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid network token"}
    assert _dispute_count(session_factory) == 0


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Network-Token": "wrong-token"}, {"X-Network-Token": ""}, OPS_HEADERS],
    ids=["missing", "wrong", "empty", "ops-token-in-the-wrong-header"],
)
def test_network_resolution_with_a_missing_or_wrong_token_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, headers: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    response = _resolve(env, "network", dispute_id, "lost", headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid network token"}
    assert _dispute_state(session_factory, dispute_id) == "under_review"
    assert _journal_count(session_factory, "dispute") == 0


def test_network_routes_reject_the_merchant_api_key_alone(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    opened = _network_open(env, payment_id, headers=env.merchant_auth)
    resolved = _resolve(env, "network", dispute_id, "won", headers=env.merchant_auth)

    # Assert
    assert (opened.status_code, resolved.status_code) == (401, 401)
    assert _dispute_state(session_factory, dispute_id) == "under_review"


@pytest.mark.parametrize("headers", [{}, NETWORK_HEADERS], ids=["no-header", "token-sent"])
def test_network_routes_return_503_when_no_network_token_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, headers: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, network_token=None)
    payment_id = _merchant_payment(env)

    # Act
    opened = _network_open(env, payment_id, headers=headers)
    resolved = _resolve(env, "network", uuid.uuid4(), "won", headers=headers)

    # Assert
    assert (opened.status_code, resolved.status_code) == (503, 503)
    assert opened.json() == {"detail": "network not configured"}
    assert resolved.json() == {"detail": "network not configured"}
    assert _dispute_count(session_factory) == 0


# --- ops route: auth -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Ops-Admin-Token": "wrong-token"}, {"X-Ops-Admin-Token": ""}, NETWORK_HEADERS],
    ids=["missing", "wrong", "empty", "network-token-in-the-wrong-header"],
)
def test_ops_resolution_with_a_missing_or_wrong_token_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, headers: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    response = _resolve(env, "ops", dispute_id, "lost", headers=headers)

    # Assert
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid ops token"}
    assert _dispute_state(session_factory, dispute_id) == "under_review"
    assert _journal_count(session_factory, "dispute") == 0


def test_ops_resolution_rejects_the_merchant_api_key_alone(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    response = _resolve(env, "ops", dispute_id, "won", headers=env.merchant_auth)

    # Assert
    assert response.status_code == 401
    assert _dispute_state(session_factory, dispute_id) == "under_review"


@pytest.mark.parametrize("headers", [{}, OPS_HEADERS], ids=["no-header", "token-sent"])
def test_ops_resolution_returns_503_when_no_ops_token_is_configured(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, headers: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, ops_admin_token=None)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    response = _resolve(env, "ops", dispute_id, "won", headers=headers)

    # Assert
    assert response.status_code == 503
    assert response.json() == {"detail": "ops not configured"}
    assert _dispute_state(session_factory, dispute_id) == "under_review"


def test_an_unconfigured_network_token_does_not_disable_the_ops_resolution(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem, network_token=None)
    opened = open_dispute(session_factory, clock, _merchant_payment(env), "fraudulent")
    submit_evidence(session_factory, clock, env.merchant_id, opened.dispute_id, "receipt attached")

    # Act
    network_attempt = _resolve(env, "network", opened.dispute_id, "won")
    ops_resolved = _resolve(env, "ops", opened.dispute_id, "won")

    # Assert
    assert network_attempt.status_code == 503
    assert ops_resolved.status_code == 200
    assert _dispute_state(session_factory, opened.dispute_id) == "won"


# --- network open ----------------------------------------------------------------------------------------


def test_network_opens_a_dispute_on_any_payment_and_gets_201_with_the_view(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env, amount_minor=2400)

    # Act
    response = _network_open(env, payment_id, reason="duplicate", amount_minor=900)

    # Assert
    view = DisputeView.model_validate(response.json())
    assert response.status_code == 201
    assert set(response.json()) == DISPUTE_VIEW_KEYS
    assert (view.payment_id, view.state, view.amount_minor, view.reason) == (
        payment_id,
        "opened",
        900,
        "duplicate",
    )
    assert _totals(session_factory, payment_id) == (0, 900, 0)


def test_network_open_records_the_network_as_the_audit_actor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)

    # Act
    dispute_id = _network_opened(env, payment_id)

    # Assert
    assert _audit(session_factory, "dispute.opened") == [
        {
            "actor_id": "network",
            "actor_role": "card_network",
            "action": "dispute.opened",
            "target": f"dispute:{dispute_id}",
        }
    ]


def test_network_open_on_an_unknown_payment_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _network_open(env, uuid.uuid4())

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "payment not found"}


def test_network_open_while_one_is_active_returns_409(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    _network_opened(env, payment_id)

    # Act
    response = _network_open(env, payment_id)

    # Assert
    assert response.status_code == 409
    assert response.json() == {"detail": "dispute already open"}
    assert _dispute_count(session_factory) == 1


def test_network_open_with_a_bad_reason_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)

    # Act
    response = _network_open(env, payment_id, reason="")

    # Assert
    assert response.status_code == 422
    assert _dispute_count(session_factory) == 0


# --- resolution (network and ops) ------------------------------------------------------------------------


@pytest.mark.parametrize("via", ["network", "ops"])
@pytest.mark.parametrize(("outcome", "fee"), [("won", 0), ("lost", 1500)])
def test_resolution_returns_200_with_the_resolved_view(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    via: Via,
    outcome: str,
    fee: int,
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    dispute_id = _under_review(env, payment_id)

    # Act
    response = _resolve(env, via, dispute_id, outcome)

    # Assert
    view = DisputeView.model_validate(response.json())
    assert response.status_code == 200
    assert set(response.json()) == DISPUTE_VIEW_KEYS
    assert (view.dispute_id, view.state, view.chargeback_fee_minor) == (dispute_id, outcome, fee)
    assert _dispute_state(session_factory, dispute_id) == outcome


@pytest.mark.parametrize(
    ("via", "actor_id", "actor_role"),
    [("network", "network", "card_network"), ("ops", "ops:admin", "ops_lead")],
)
def test_resolution_audit_row_carries_the_route_specific_actor_and_role(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    via: Via,
    actor_id: str,
    actor_role: str,
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    _resolve(env, via, dispute_id, "lost")

    # Assert
    assert _audit(session_factory, "dispute.resolved") == [
        {
            "actor_id": actor_id,
            "actor_role": actor_role,
            "action": "dispute.resolved",
            "target": f"dispute:{dispute_id}",
        }
    ]


@pytest.mark.parametrize("via", ["network", "ops"])
@pytest.mark.parametrize(("outcome", "flag"), [("lost", True), ("won", False)])
def test_chargeback_flag_is_set_only_when_the_dispute_is_lost(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    via: Via,
    outcome: str,
    flag: bool,
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    dispute_id = _under_review(env, payment_id)
    flag_before = _chargeback_flag(session_factory, payment_id)

    # Act
    _resolve(env, via, dispute_id, outcome)

    # Assert
    assert flag_before is False
    assert _chargeback_flag(session_factory, payment_id) is flag
    assert _totals(session_factory, payment_id) == (0, 0, 1500 if outcome == "lost" else 0)


def test_the_chargeback_flag_stays_true_after_a_later_dispute_on_the_payment_is_won(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env, amount_minor=3000)
    first = _network_open(env, payment_id, amount_minor=1000).json()["dispute_id"]
    _evidence(env, uuid.UUID(first))
    _resolve(env, "network", uuid.UUID(first), "lost")
    second = _network_open(env, payment_id, amount_minor=500).json()["dispute_id"]
    _evidence(env, uuid.UUID(second))

    # Act
    response = _resolve(env, "network", uuid.UUID(second), "won")

    # Assert
    assert response.status_code == 200
    assert _chargeback_flag(session_factory, payment_id) is True
    assert _totals(session_factory, payment_id) == (0, 0, 1000)


@pytest.mark.parametrize("via", ["network", "ops"])
@pytest.mark.parametrize("outcome", ["won", "lost"])
def test_resolving_an_opened_dispute_returns_409_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, via: Via, outcome: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    dispute_id = _network_opened(env, payment_id)

    # Act
    response = _resolve(env, via, dispute_id, outcome)

    # Assert
    assert response.status_code == 409
    assert response.json() == INVALID_TRANSITION
    assert _dispute_state(session_factory, dispute_id) == "opened"
    assert _totals(session_factory, payment_id) == (0, 1500, 0)
    assert _chargeback_flag(session_factory, payment_id) is False


@pytest.mark.parametrize("via", ["network", "ops"])
@pytest.mark.parametrize("second_outcome", ["won", "lost"])
def test_resolving_twice_returns_409_and_posts_no_second_journal(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    via: Via,
    second_outcome: str,
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    dispute_id = _under_review(env, payment_id)
    first = _resolve(env, via, dispute_id, "lost")
    assert first.status_code == 200

    # Act
    second = _resolve(env, via, dispute_id, second_outcome)

    # Assert
    assert second.status_code == 409
    assert second.json() == INVALID_TRANSITION
    assert _dispute_state(session_factory, dispute_id) == "lost"
    assert _journal_count(session_factory, "dispute") == 1
    assert _totals(session_factory, payment_id) == (0, 0, 1500)


@pytest.mark.parametrize("via", ["network", "ops"])
def test_resolving_an_unknown_dispute_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, via: Via
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _resolve(env, via, uuid.uuid4(), "won")

    # Assert
    assert response.status_code == 404
    assert response.json() == {"detail": "dispute not found"}


@pytest.mark.parametrize("via", ["network", "ops"])
@pytest.mark.parametrize("outcome", ["refunded", "under_review", "", "WON"])
def test_resolving_with_an_unknown_outcome_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, via: Via, outcome: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    response = _resolve(env, via, dispute_id, outcome)

    # Assert
    assert response.status_code == 422
    assert _dispute_state(session_factory, dispute_id) == "under_review"


# --- refund + dispute + chargeback never exceed the payment amount -------------------------------------


def test_refund_and_a_lost_dispute_together_never_exceed_the_payment_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id, amount_minor=1500)
    assert _refund(env, payment_id, 500).status_code == 201
    dispute_id = _consumer_opened(env, user_id, payment_id)
    _evidence(env, dispute_id)
    _resolve(env, "network", dispute_id, "lost")

    # Act
    extra_refund = _refund(env, payment_id, 1)
    reopened = _consumer_open(env, user_id, payment_id)

    # Assert
    refunded, disputed, chargeback = _totals(session_factory, payment_id)
    assert (refunded, disputed, chargeback) == (500, 0, 1000)
    assert refunded + disputed + chargeback <= 1500
    assert extra_refund.status_code == 409
    assert extra_refund.json()["error"] == "refund_exceeds_refundable"
    assert reopened.status_code == 409
    assert reopened.json() == {"detail": "dispute amount exceeds"}


def test_a_refund_after_a_partial_dispute_may_only_take_what_the_dispute_leaves(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env, amount_minor=1500)
    _network_open(env, payment_id, amount_minor=700)

    # Act
    too_much = _refund(env, payment_id, 801)
    exact = _refund(env, payment_id, 800)

    # Assert
    assert too_much.status_code == 409
    assert exact.status_code == 201
    assert _totals(session_factory, payment_id) == (800, 700, 0)


def test_a_dispute_after_a_partial_refund_cannot_take_more_than_what_is_left(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env, amount_minor=1500)
    assert _refund(env, payment_id, 500).status_code == 201

    # Act
    above = _network_open(env, payment_id, amount_minor=1001)
    exact = _network_open(env, payment_id, amount_minor=1000)

    # Assert
    assert above.status_code == 409
    assert exact.status_code == 201
    assert _totals(session_factory, payment_id) == (500, 1000, 0)


# --- notifications raised by the dispute routes -----------------------------------------------------------


def test_a_consumer_dispute_notifies_the_merchant_with_dispute_opened(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    _consumer_opened(env, user_id, payment_id)

    # Assert
    assert _notices(session_factory, "merchant", env.merchant_id, "dispute_opened") == [
        {
            "kind": "dispute_opened",
            "payment_id": payment_id,
            "recipient_type": "merchant",
            "recipient_id": env.merchant_id,
        }
    ]


def test_a_network_dispute_notifies_only_the_owning_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    other_id, _ = _add_merchant(env, "Other")
    payment_id = _merchant_payment(env)

    # Act
    _network_opened(env, payment_id)

    # Assert
    assert len(_notices(session_factory, "merchant", env.merchant_id, "dispute_opened")) == 1
    assert _notices(session_factory, "merchant", other_id, "dispute_opened") == []


def test_a_rejected_open_creates_no_dispute_opened_notification(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    _network_opened(env, payment_id)

    # Act
    rejected = _network_open(env, payment_id)

    # Assert
    assert rejected.status_code == 409
    assert len(_notices(session_factory, "merchant", env.merchant_id, "dispute_opened")) == 1


@pytest.mark.parametrize("via", ["network", "ops"])
@pytest.mark.parametrize("outcome", ["won", "lost"])
def test_resolving_a_dispute_notifies_the_merchant_and_the_payer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, via: Via, outcome: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    dispute_id = _consumer_opened(env, user_id, payment_id)
    _evidence(env, dispute_id)

    # Act
    response = _resolve(env, via, dispute_id, outcome)

    # Assert
    assert response.status_code == 200
    assert _notices(session_factory, "merchant", env.merchant_id, "dispute_resolved") == [
        {
            "kind": "dispute_resolved",
            "payment_id": payment_id,
            "recipient_type": "merchant",
            "recipient_id": env.merchant_id,
        }
    ]
    assert _notices(session_factory, "user", user_id, "dispute_update") == [
        {
            "kind": "dispute_update",
            "payment_id": payment_id,
            "recipient_type": "user",
            "recipient_id": user_id,
        }
    ]


def test_resolving_a_merchant_only_payments_dispute_creates_no_user_notification(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    dispute_id = _under_review(env, _merchant_payment(env))

    # Act
    _resolve(env, "network", dispute_id, "lost")

    # Assert
    user_records = _rows(
        session_factory, "SELECT count(*) AS n FROM gateway.notifications WHERE recipient_type = 'user'"
    )
    assert len(_notices(session_factory, "merchant", env.merchant_id, "dispute_resolved")) == 1
    assert user_records[0]["n"] == 0


def test_a_rejected_resolution_creates_no_notification(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    dispute_id = _consumer_opened(env, user_id, _paid(env, user_id))

    # Act
    rejected = _resolve(env, "network", dispute_id, "lost")

    # Assert
    assert rejected.status_code == 409
    assert _notices(session_factory, "merchant", env.merchant_id, "dispute_resolved") == []
    assert _notices(session_factory, "user", user_id, "dispute_update") == []


def test_a_dispute_update_for_the_payer_writes_no_notification_outbox_row(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    dispute_id = _consumer_opened(env, user_id, _paid(env, user_id))
    _evidence(env, dispute_id)

    # Act
    _resolve(env, "ops", dispute_id, "won")

    # Assert
    user_rows = _rows(
        session_factory,
        "SELECT count(*) AS n FROM gateway.outbox "
        "WHERE topic = :topic AND payload->>'recipient_type' = 'user'",
        topic=NOTIFICATIONS_TOPIC,
    )
    assert _notices(session_factory, "user", user_id, "dispute_update")[0]["kind"] == "dispute_update"
    assert user_rows[0]["n"] == 0
