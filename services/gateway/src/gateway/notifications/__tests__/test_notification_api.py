"""RED tests for user notifications and the notification APIs (B11.1, B11.2), from
docs/specs/disputes-notifications.md section 4: the notifier's user-recipient records, the read APIs for the
user and the merchant, mark-read, and the was-this-you response endpoint.
"""

import json
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from contracts.events import NOTIFICATIONS_TOPIC
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant, sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.notifications.service import process_pending_notifications
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import open_dispute
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.refunds import refund_payment
from gateway.payments.schemas import ChargeRequest
from httpx import Response
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"
HIGH_USD = 500_000
PASSWORD = "notification-api-password-7Q"
RECEIPT = "payment_receipt"
WAS_THIS_YOU = "was_this_you"
NOTIFICATION_VIEW_KEYS = {
    "notification_id",
    "kind",
    "payment_id",
    "payload",
    "created_at",
    "read",
    "response",
}
PAGE_KEYS = {"items", "next_cursor"}
NOT_FOUND = {"detail": "notification not found"}
FORBIDDEN_FRAGMENTS = ("4242", "@", "card_bin", "card_last4", "email")
UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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


def _pay(env: Env, user_id: uuid.UUID, pan: str = APPROVE_PAN, amount_minor: int = 1500) -> Response:
    response: Response = env.client.post(
        "/v1/me/payments",
        json={
            "merchant_id": str(env.merchant_id),
            "amount_minor": amount_minor,
            "currency": "USD",
            "card": _card(pan),
        },
        headers={**_user_auth(env, user_id), "Idempotency-Key": uuid.uuid4().hex},
    )
    return response


def _paid(env: Env, user_id: uuid.UUID, amount_minor: int = 1500) -> uuid.UUID:
    response = _pay(env, user_id, amount_minor=amount_minor)
    assert response.status_code == 201
    assert response.json()["state"] == "succeeded"
    return uuid.UUID(str(response.json()["payment_id"]))


def _merchant_payment(env: Env, pan: str = APPROVE_PAN, amount_minor: int = 1500) -> uuid.UUID:
    request = ChargeRequest.model_validate(
        {"amount_minor": amount_minor, "currency": "USD", "method": "card", "card": _card(pan)}
    )
    outcome = charge_card(env.deps, env.merchant_id, uuid.uuid4().hex, request)
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _prime(env: Env, pan: str, count: int) -> None:
    """`count` small merchant charges one minute apart on one card; the clock ends `count` minutes later."""
    for _ in range(count):
        _merchant_payment(env, pan=pan)
        env.clock.advance(timedelta(minutes=1))


def _flagged_payment(env: Env, user_id: uuid.UUID) -> uuid.UUID:
    """Three earlier charges on the card (velocity) plus a high amount: flagged but not blocked, succeeded."""
    _prime(env, APPROVE_PAN, 3)
    payment_id = _paid(env, user_id, amount_minor=HIGH_USD)
    assert _payment_flags(env.session_factory, payment_id) == (True, False)
    return payment_id


def _blocked_payment(env: Env, user_id: uuid.UUID) -> uuid.UUID:
    """Three earlier declines on the card plus a high amount: declined as risk_blocked before the provider."""
    _prime(env, DECLINED_PAN, 3)
    response = _pay(env, user_id, pan=DECLINED_PAN, amount_minor=HIGH_USD)
    assert response.status_code == 201
    assert (response.json()["state"], response.json()["decline_code"]) == ("declined", "risk_blocked")
    return uuid.UUID(str(response.json()["payment_id"]))


def _notify(env: Env) -> int:
    return process_pending_notifications(env.session_factory, env.clock)


def _rows(session_factory: sessionmaker[Session], query: str, **params: object) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(query), params).mappings()]


def _payment_flags(session_factory: sessionmaker[Session], payment_id: uuid.UUID) -> tuple[bool, bool]:
    row = _rows(
        session_factory,
        "SELECT risk_flagged, risk_blocked FROM gateway.payments WHERE id = :id",
        id=payment_id,
    )[0]
    return row["risk_flagged"] is True, row["risk_blocked"] is True


def _records(
    session_factory: sessionmaker[Session], recipient_type: str, recipient_id: uuid.UUID
) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, kind, payment_id, payload, created_at, read_at, response, responded_at "
        "FROM gateway.notifications WHERE recipient_type = :rtype AND recipient_id = :rid "
        "ORDER BY created_at, id",
        rtype=recipient_type,
        rid=recipient_id,
    )


def _kinds(records: list[dict[str, object]]) -> list[str]:
    return sorted(str(record["kind"]) for record in records)


def _notice_id(
    session_factory: sessionmaker[Session], recipient_type: str, recipient_id: uuid.UUID, kind: str
) -> uuid.UUID:
    matching = [r for r in _records(session_factory, recipient_type, recipient_id) if r["kind"] == kind]
    assert len(matching) == 1
    return uuid.UUID(str(matching[0]["id"]))


def _disputes(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT id, payment_id, reason, amount_minor, state FROM gateway.disputes ORDER BY created_at, id",
    )


def _audit(session_factory: sessionmaker[Session], action: str) -> list[dict[str, object]]:
    return _rows(
        session_factory,
        "SELECT actor_id, actor_role, action FROM gateway.audit_log WHERE action = :action ORDER BY id",
        action=action,
    )


def _without_ids(value: str) -> str:
    return UUID_PATTERN.sub("<id>", value)


def _list_user(env: Env, user_id: uuid.UUID, **params: str | int) -> Response:
    response: Response = env.client.get(
        "/v1/me/notifications", params=dict(params), headers=_user_auth(env, user_id)
    )
    return response


def _list_merchant(env: Env, auth: dict[str, str] | None = None, **params: str | int) -> Response:
    response: Response = env.client.get(
        "/v1/notifications", params=dict(params), headers=env.merchant_auth if auth is None else auth
    )
    return response


def _answer(env: Env, user_id: uuid.UUID, notification_id: uuid.UUID, answer: str) -> Response:
    response: Response = env.client.post(
        f"/v1/me/notifications/{notification_id}/response",
        json={"answer": answer},
        headers=_user_auth(env, user_id),
    )
    return response


def _was_this_you_for_flagged(env: Env, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """(payment id, was_this_you notification id) for a flagged, succeeded payer payment."""
    payment_id = _flagged_payment(env, user_id)
    _notify(env)
    return payment_id, _notice_id(env.session_factory, "user", user_id, WAS_THIS_YOU)


# --- notifier: user-recipient records ----------------------------------------------------------------------


def test_a_succeeded_payer_payment_creates_a_payment_receipt_for_the_user(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)

    # Act
    _notify(env)

    # Assert
    records = _records(session_factory, "user", user_id)
    assert [(r["kind"], r["payment_id"]) for r in records] == [(RECEIPT, payment_id)]
    assert _kinds(_records(session_factory, "merchant", env.merchant_id)) == ["payment_confirmation"]


def test_a_user_record_carries_no_card_or_email_data(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _flagged_payment(env, user_id)

    # Act
    _notify(env)

    # Assert
    payloads = [r["payload"] for r in _records(session_factory, "user", user_id)]
    visible = [_without_ids(json.dumps(payload)).lower() for payload in payloads]
    leaked = [fragment for text_ in visible for fragment in FORBIDDEN_FRAGMENTS if fragment in text_]
    assert len(visible) == 2
    assert leaked == []


def test_user_records_write_no_notification_outbox_rows(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _paid(env, user_id)

    # Act
    _notify(env)

    # Assert
    outbox = _rows(
        session_factory,
        "SELECT payload->>'recipient_type' AS recipient_type FROM gateway.outbox WHERE topic = :topic",
        topic=NOTIFICATIONS_TOPIC,
    )
    assert _kinds(_records(session_factory, "user", user_id)) == [RECEIPT]
    assert [row["recipient_type"] for row in outbox] == ["merchant"]


def test_a_flagged_succeeded_payer_payment_creates_a_receipt_and_a_was_this_you(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _flagged_payment(env, user_id)

    # Act
    _notify(env)

    # Assert
    records = _records(session_factory, "user", user_id)
    assert _kinds(records) == [RECEIPT, WAS_THIS_YOU]
    assert {r["payment_id"] for r in records} == {payment_id}


def test_a_risk_blocked_payer_payment_creates_only_a_was_this_you(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _blocked_payment(env, user_id)

    # Act
    _notify(env)

    # Assert
    records = _records(session_factory, "user", user_id)
    assert [(r["kind"], r["payment_id"]) for r in records] == [(WAS_THIS_YOU, payment_id)]


def test_a_plain_decline_of_a_payer_payment_creates_no_user_record(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    declined = _pay(env, user_id, pan=DECLINED_PAN)
    assert declined.json()["decline_code"] == "card_declined"

    # Act
    _notify(env)

    # Assert
    assert _records(session_factory, "user", user_id) == []


def test_merchant_only_payments_create_no_user_records(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _prime(env, APPROVE_PAN, 3)
    flagged = _merchant_payment(env, amount_minor=HIGH_USD)
    assert _payment_flags(session_factory, flagged) == (True, False)

    # Act
    _notify(env)

    # Assert
    user_rows = _rows(
        session_factory, "SELECT count(*) AS n FROM gateway.notifications WHERE recipient_type = 'user'"
    )
    merchant_kinds = _kinds(_records(session_factory, "merchant", env.merchant_id))
    assert user_rows[0]["n"] == 0
    assert merchant_kinds == ["payment_confirmation"] * 4 + ["suspicious_activity"]


def test_user_records_go_only_to_the_payer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payer = _add_user(env, "payer@example.com")
    bystander = _add_user(env, "bystander@example.com")
    _paid(env, payer)

    # Act
    _notify(env)

    # Assert
    assert _kinds(_records(session_factory, "user", payer)) == [RECEIPT]
    assert _records(session_factory, "user", bystander) == []


def test_a_second_notifier_pass_creates_no_further_user_records(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _flagged_payment(env, user_id)
    _notify(env)
    before = _records(session_factory, "user", user_id)

    # Act
    created = _notify(env)

    # Assert
    assert created == 0
    assert _records(session_factory, "user", user_id) == before


# --- GET /v1/me/notifications ---------------------------------------------------------------------------


def test_my_notifications_returns_the_notification_view_with_exactly_the_specified_fields(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id = _paid(env, user_id)
    _notify(env)
    record = _records(session_factory, "user", user_id)[0]

    # Act
    response = _list_user(env, user_id)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == PAGE_KEYS
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == NOTIFICATION_VIEW_KEYS
    assert body["items"][0] == {
        "notification_id": str(record["id"]),
        "kind": RECEIPT,
        "payment_id": str(payment_id),
        "payload": record["payload"],
        "created_at": body["items"][0]["created_at"],
        "read": False,
        "response": None,
    }
    assert body["next_cursor"] is None


def test_my_notifications_lists_only_the_users_own_records(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    alice = _add_user(env, "alice@example.com")
    bob = _add_user(env, "bob@example.com")
    alice_payment = _paid(env, alice)
    _paid(env, bob)
    _notify(env)

    # Act
    response = _list_user(env, alice)

    # Assert
    assert [(item["kind"], item["payment_id"]) for item in response.json()["items"]] == [
        (RECEIPT, str(alice_payment))
    ]


def test_my_notifications_never_lists_the_merchants_records(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _merchant_payment(env)
    _notify(env)

    # Act
    response = _list_user(env, user_id)

    # Assert
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_my_notifications_are_newest_first_and_page_with_a_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payments: list[uuid.UUID] = []
    for _ in range(3):
        payments.append(_paid(env, user_id))
        _notify(env)
        clock.advance(timedelta(minutes=1))

    # Act
    page_one = _list_user(env, user_id, limit=2).json()
    page_two = _list_user(env, user_id, limit=2, cursor=page_one["next_cursor"]).json()

    # Assert
    assert [item["payment_id"] for item in page_one["items"]] == [str(payments[2]), str(payments[1])]
    assert page_one["next_cursor"] is not None
    assert [item["payment_id"] for item in page_two["items"]] == [str(payments[0])]
    assert page_two["next_cursor"] is None


def test_my_notifications_unread_only_hides_records_marked_read(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    read_payment = _paid(env, user_id)
    _notify(env)
    clock.advance(timedelta(minutes=1))
    unread_payment = _paid(env, user_id)
    _notify(env)
    read_id = next(
        r["id"] for r in _records(session_factory, "user", user_id) if r["payment_id"] == read_payment
    )
    env.client.post(f"/v1/me/notifications/{read_id}/read", headers=_user_auth(env, user_id))

    # Act
    unread = _list_user(env, user_id, unread_only="true").json()["items"]
    everything = _list_user(env, user_id).json()["items"]

    # Assert
    assert [item["payment_id"] for item in unread] == [str(unread_payment)]
    assert [(item["payment_id"], item["read"]) for item in everything] == [
        (str(unread_payment), False),
        (str(read_payment), True),
    ]


def test_my_notifications_rejects_a_bad_cursor_with_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _list_user(env, user_id, cursor="not-a-cursor")

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


@pytest.mark.parametrize("limit", [0, 101])
def test_my_notifications_rejects_a_limit_outside_1_to_100_with_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, limit: int
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    response = _list_user(env, user_id, limit=limit)

    # Assert
    assert response.status_code == 422


def test_my_notifications_without_a_valid_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    anonymous = env.client.get("/v1/me/notifications")
    with_api_key = env.client.get("/v1/me/notifications", headers=env.merchant_auth)

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)


# --- GET /v1/notifications (merchant) ------------------------------------------------------------------


def test_merchant_notifications_returns_the_view_with_exactly_the_specified_fields(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payment_id = _merchant_payment(env)
    _notify(env)
    record = _records(session_factory, "merchant", env.merchant_id)[0]

    # Act
    response = _list_merchant(env)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == PAGE_KEYS
    assert len(body["items"]) == 1
    assert set(body["items"][0]) == NOTIFICATION_VIEW_KEYS
    assert body["items"][0] == {
        "notification_id": str(record["id"]),
        "kind": "payment_confirmation",
        "payment_id": str(payment_id),
        "payload": {"amount_minor": 1500, "currency": "USD"},
        "created_at": body["items"][0]["created_at"],
        "read": False,
        "response": None,
    }


def test_merchant_notifications_lists_only_the_callers_records_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    other_id, other_auth = _add_merchant(env, "Other")
    first = _merchant_payment(env)
    _notify(env)
    clock.advance(timedelta(minutes=1))
    request = ChargeRequest.model_validate(
        {"amount_minor": 900, "currency": "USD", "method": "card", "card": _card(APPROVE_PAN)}
    )
    charge_card(env.deps, other_id, uuid.uuid4().hex, request)
    second = _merchant_payment(env)
    _notify(env)

    # Act
    own = _list_merchant(env)
    others = _list_merchant(env, other_auth)

    # Assert
    assert [item["payment_id"] for item in own.json()["items"]] == [str(second), str(first)]
    assert len(others.json()["items"]) == 1


def test_merchant_notifications_never_include_user_records(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _flagged_payment(env, user_id)
    _notify(env)

    # Act
    items = _list_merchant(env).json()["items"]

    # Assert
    assert len(_records(session_factory, "user", user_id)) == 2
    assert {item["kind"] for item in items} == {"payment_confirmation", "suspicious_activity"}


def test_merchant_notifications_page_and_filter_unread(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    payments: list[uuid.UUID] = []
    for _ in range(3):
        payments.append(_merchant_payment(env))
        _notify(env)
        clock.advance(timedelta(minutes=1))
    records = _records(session_factory, "merchant", env.merchant_id)
    oldest_id = records[0]["id"]
    env.client.post(f"/v1/notifications/{oldest_id}/read", headers=env.merchant_auth)

    # Act
    page_one = _list_merchant(env, limit=2).json()
    page_two = _list_merchant(env, limit=2, cursor=page_one["next_cursor"]).json()
    unread = _list_merchant(env, unread_only="true").json()["items"]

    # Assert
    assert [item["payment_id"] for item in page_one["items"]] == [str(payments[2]), str(payments[1])]
    assert [item["payment_id"] for item in page_two["items"]] == [str(payments[0])]
    assert page_two["next_cursor"] is None
    assert [item["payment_id"] for item in unread] == [str(payments[2]), str(payments[1])]


def test_merchant_notifications_reject_a_bad_cursor_with_400(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)

    # Act
    response = _list_merchant(env, cursor="not-a-cursor")

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


def test_merchant_notifications_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")

    # Act
    anonymous = env.client.get("/v1/notifications")
    wrong_key = env.client.get("/v1/notifications", headers={"Authorization": "Bearer sk_test_x"})
    with_session = env.client.get("/v1/notifications", headers=_user_auth(env, user_id))

    # Assert
    assert (anonymous.status_code, wrong_key.status_code, with_session.status_code) == (401, 401, 401)


# --- POST .../read --------------------------------------------------------------------------------------


def test_marking_a_user_notification_read_returns_the_view_with_read_true(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _paid(env, user_id)
    _notify(env)
    notification_id = _notice_id(session_factory, "user", user_id, RECEIPT)

    # Act
    response = env.client.post(
        f"/v1/me/notifications/{notification_id}/read", headers=_user_auth(env, user_id)
    )

    # Assert
    row = _records(session_factory, "user", user_id)[0]
    assert response.status_code == 200
    assert set(response.json()) == NOTIFICATION_VIEW_KEYS
    assert (response.json()["notification_id"], response.json()["read"]) == (str(notification_id), True)
    assert row["read_at"] == clock()


def test_marking_read_twice_is_idempotent_and_keeps_the_first_read_time(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _paid(env, user_id)
    _notify(env)
    notification_id = _notice_id(session_factory, "user", user_id, RECEIPT)
    url = f"/v1/me/notifications/{notification_id}/read"
    first = env.client.post(url, headers=_user_auth(env, user_id))
    first_read_at = _records(session_factory, "user", user_id)[0]["read_at"]
    clock.advance(timedelta(minutes=5))

    # Act
    second = env.client.post(url, headers=_user_auth(env, user_id))

    # Assert
    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert _records(session_factory, "user", user_id)[0]["read_at"] == first_read_at


def test_marking_another_users_notification_read_returns_404_and_changes_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    owner = _add_user(env, "owner@example.com")
    stranger = _add_user(env, "stranger@example.com")
    _paid(env, owner)
    _notify(env)
    notification_id = _notice_id(session_factory, "user", owner, RECEIPT)

    # Act
    response = env.client.post(
        f"/v1/me/notifications/{notification_id}/read", headers=_user_auth(env, stranger)
    )

    # Assert
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert _records(session_factory, "user", owner)[0]["read_at"] is None


def test_marking_an_unknown_or_merchant_notification_read_through_the_user_route_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _merchant_payment(env)
    _notify(env)
    merchant_record = _records(session_factory, "merchant", env.merchant_id)[0]["id"]

    # Act
    unknown = env.client.post(f"/v1/me/notifications/{uuid.uuid4()}/read", headers=_user_auth(env, user_id))
    merchants = env.client.post(
        f"/v1/me/notifications/{merchant_record}/read", headers=_user_auth(env, user_id)
    )

    # Assert
    assert (unknown.status_code, merchants.status_code) == (404, 404)
    assert unknown.json() == NOT_FOUND
    assert merchants.json() == NOT_FOUND
    assert _records(session_factory, "merchant", env.merchant_id)[0]["read_at"] is None


def test_marking_a_merchant_notification_read_returns_the_view_with_read_true(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _merchant_payment(env)
    _notify(env)
    notification_id = _records(session_factory, "merchant", env.merchant_id)[0]["id"]
    url = f"/v1/notifications/{notification_id}/read"

    # Act
    first = env.client.post(url, headers=env.merchant_auth)
    second = env.client.post(url, headers=env.merchant_auth)

    # Assert
    assert (first.status_code, second.status_code) == (200, 200)
    assert set(first.json()) == NOTIFICATION_VIEW_KEYS
    assert (first.json()["notification_id"], first.json()["read"]) == (str(notification_id), True)
    assert second.json() == first.json()


def test_marking_another_merchants_or_a_users_notification_read_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    _, other_auth = _add_merchant(env, "Other")
    user_id = _add_user(env, "payer@example.com")
    _paid(env, user_id)
    _notify(env)
    merchant_record = _records(session_factory, "merchant", env.merchant_id)[0]["id"]
    user_record = _records(session_factory, "user", user_id)[0]["id"]

    # Act
    other_merchant = env.client.post(f"/v1/notifications/{merchant_record}/read", headers=other_auth)
    user_record_by_merchant = env.client.post(
        f"/v1/notifications/{user_record}/read", headers=env.merchant_auth
    )
    unknown = env.client.post(f"/v1/notifications/{uuid.uuid4()}/read", headers=env.merchant_auth)

    # Assert
    assert [r.status_code for r in (other_merchant, user_record_by_merchant, unknown)] == [404, 404, 404]
    assert other_merchant.json() == NOT_FOUND
    assert user_record_by_merchant.json() == NOT_FOUND
    assert _records(session_factory, "user", user_id)[0]["read_at"] is None


def test_mark_read_routes_reject_a_non_uuid_id_with_422_and_missing_credentials_with_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    some_id = uuid.uuid4()

    # Act
    bad_user_id = env.client.post("/v1/me/notifications/not-a-uuid/read", headers=_user_auth(env, user_id))
    bad_merchant_id = env.client.post("/v1/notifications/not-a-uuid/read", headers=env.merchant_auth)
    anonymous_user = env.client.post(f"/v1/me/notifications/{some_id}/read")
    anonymous_merchant = env.client.post(f"/v1/notifications/{some_id}/read")

    # Assert
    assert (bad_user_id.status_code, bad_merchant_id.status_code) == (422, 422)
    assert (anonymous_user.status_code, anonymous_merchant.status_code) == (401, 401)


# --- POST /v1/me/notifications/{id}/response -----------------------------------------------------------


def test_confirming_a_was_this_you_returns_the_answer_without_a_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _, notification_id = _was_this_you_for_flagged(env, user_id)

    # Act
    response = _answer(env, user_id, notification_id, "confirmed")

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "notification_id": str(notification_id),
        "response": "confirmed",
        "dispute_id": None,
    }
    assert _disputes(session_factory) == []


def test_answering_marks_the_notification_read_and_records_the_response(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _, notification_id = _was_this_you_for_flagged(env, user_id)

    # Act
    _answer(env, user_id, notification_id, "confirmed")

    # Assert
    items = _list_user(env, user_id).json()["items"]
    answered = next(item for item in items if item["notification_id"] == str(notification_id))
    row = next(r for r in _records(session_factory, "user", user_id) if r["id"] == notification_id)
    assert (answered["kind"], answered["read"], answered["response"]) == (WAS_THIS_YOU, True, "confirmed")
    assert (row["read_at"], row["responded_at"]) == (clock(), clock())


def test_not_me_on_a_flagged_succeeded_payment_opens_a_dispute_for_the_full_amount(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id, notification_id = _was_this_you_for_flagged(env, user_id)

    # Act
    response = _answer(env, user_id, notification_id, "not_me")

    # Assert
    disputes = _disputes(session_factory)
    assert response.status_code == 200
    assert len(disputes) == 1
    assert response.json() == {
        "notification_id": str(notification_id),
        "response": "not_me",
        "dispute_id": str(disputes[0]["id"]),
    }
    dispute = disputes[0]
    assert (dispute["payment_id"], dispute["reason"], dispute["amount_minor"], dispute["state"]) == (
        payment_id,
        "fraudulent_not_me",
        HIGH_USD,
        "opened",
    )


def test_not_me_opens_the_dispute_as_the_user_with_the_consumer_role(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id, notification_id = _was_this_you_for_flagged(env, user_id)

    # Act
    _answer(env, user_id, notification_id, "not_me")

    # Assert
    totals = _rows(
        env.session_factory, "SELECT disputed_minor FROM gateway.payments WHERE id = :id", id=payment_id
    )[0]
    assert _audit(session_factory, "dispute.opened") == [
        {"actor_id": f"user:{user_id}", "actor_role": "consumer", "action": "dispute.opened"}
    ]
    assert totals["disputed_minor"] == HIGH_USD


def test_not_me_disputes_only_what_is_still_refundable(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id, notification_id = _was_this_you_for_flagged(env, user_id)
    refund_payment(session_factory, clock, env.merchant_id, payment_id, "partial-refund", 100_000)

    # Act
    response = _answer(env, user_id, notification_id, "not_me")

    # Assert
    disputes = _disputes(session_factory)
    assert response.status_code == 200
    assert [d["amount_minor"] for d in disputes] == [HIGH_USD - 100_000]


def test_not_me_on_a_risk_blocked_payment_records_the_answer_and_opens_no_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _blocked_payment(env, user_id)
    _notify(env)
    notification_id = _notice_id(session_factory, "user", user_id, WAS_THIS_YOU)

    # Act
    response = _answer(env, user_id, notification_id, "not_me")

    # Assert
    assert response.status_code == 200
    assert response.json() == {
        "notification_id": str(notification_id),
        "response": "not_me",
        "dispute_id": None,
    }
    assert _disputes(session_factory) == []


def test_not_me_while_a_dispute_is_already_active_records_the_answer_without_a_second_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    payment_id, notification_id = _was_this_you_for_flagged(env, user_id)
    existing = open_dispute(session_factory, clock, payment_id, "fraudulent")

    # Act
    response = _answer(env, user_id, notification_id, "not_me")

    # Assert
    assert response.status_code == 200
    assert response.json()["response"] == "not_me"
    assert response.json()["dispute_id"] is None
    assert [d["id"] for d in _disputes(session_factory)] == [existing.dispute_id]


@pytest.mark.parametrize(
    ("first", "second"),
    [("confirmed", "confirmed"), ("confirmed", "not_me"), ("not_me", "confirmed"), ("not_me", "not_me")],
)
def test_a_second_answer_returns_409_and_keeps_the_first(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, first: str, second: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _, notification_id = _was_this_you_for_flagged(env, user_id)
    assert _answer(env, user_id, notification_id, first).status_code == 200
    disputes_after_first = _disputes(session_factory)

    # Act
    response = _answer(env, user_id, notification_id, second)

    # Assert
    row = next(r for r in _records(session_factory, "user", user_id) if r["id"] == notification_id)
    assert response.status_code == 409
    assert response.json() == {"detail": "already answered"}
    assert row["response"] == first
    assert _disputes(session_factory) == disputes_after_first


def test_answering_a_notice_that_is_not_a_was_this_you_returns_409(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _paid(env, user_id)
    _notify(env)
    receipt_id = _notice_id(session_factory, "user", user_id, RECEIPT)

    # Act
    response = _answer(env, user_id, receipt_id, "not_me")

    # Assert
    assert response.status_code == 409
    assert response.json() == {"detail": "not a was-this-you notice"}
    assert _records(session_factory, "user", user_id)[0]["response"] is None
    assert _disputes(session_factory) == []


def test_answering_another_users_was_this_you_returns_404_and_opens_no_dispute(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    owner = _add_user(env, "owner@example.com")
    stranger = _add_user(env, "stranger@example.com")
    _, notification_id = _was_this_you_for_flagged(env, owner)

    # Act
    response = _answer(env, stranger, notification_id, "not_me")

    # Assert
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert _disputes(session_factory) == []


def test_answering_an_unknown_or_merchant_notification_returns_404(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _merchant_payment(env)
    _notify(env)
    merchant_record = uuid.UUID(str(_records(session_factory, "merchant", env.merchant_id)[0]["id"]))

    # Act
    unknown = _answer(env, user_id, uuid.uuid4(), "confirmed")
    merchants = _answer(env, user_id, merchant_record, "confirmed")

    # Assert
    assert (unknown.status_code, merchants.status_code) == (404, 404)
    assert unknown.json() == NOT_FOUND
    assert merchants.json() == NOT_FOUND


@pytest.mark.parametrize("body", [{"answer": "maybe"}, {"answer": ""}, {"answer": "NOT_ME"}, {}])
def test_answering_with_an_unknown_answer_returns_422_and_records_nothing(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, body: dict[str, str]
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _, notification_id = _was_this_you_for_flagged(env, user_id)

    # Act
    response = env.client.post(
        f"/v1/me/notifications/{notification_id}/response", json=body, headers=_user_auth(env, user_id)
    )

    # Assert
    row = next(r for r in _records(session_factory, "user", user_id) if r["id"] == notification_id)
    assert response.status_code == 422
    assert row["response"] is None
    assert _disputes(session_factory) == []


def test_answering_without_a_valid_session_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    env = _env(session_factory, clock, private_pem)
    user_id = _add_user(env, "payer@example.com")
    _, notification_id = _was_this_you_for_flagged(env, user_id)
    url = f"/v1/me/notifications/{notification_id}/response"

    # Act
    anonymous = env.client.post(url, json={"answer": "not_me"})
    with_api_key = env.client.post(url, json={"answer": "not_me"}, headers=env.merchant_auth)

    # Assert
    assert (anonymous.status_code, with_api_key.status_code) == (401, 401)
    assert _disputes(session_factory) == []
