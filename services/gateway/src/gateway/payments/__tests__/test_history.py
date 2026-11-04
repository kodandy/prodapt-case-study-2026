"""RED tests for B3 history, written from docs/specs/gateway-basic.md (history.py + GET /v1/payments)."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.disputes import PaymentNotFoundError
from gateway.payments.history import (
    PAYMENT_METHODS,
    InvalidCursorError,
    PaymentPage,
    get_payment,
    list_payments,
)
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import CardSummary, ChargeRequest, PaymentSummary
from gateway.payments.state import PaymentState
from gateway.payments.tokenization.cards import card_fingerprint
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
DECLINED_PAN = "4000000000000002"


@dataclass(frozen=True)
class Harness:
    deps: ChargeDeps
    merchant_id: uuid.UUID
    auth: dict[str, str]


def _harness(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    deps: ChargeDeps | None = None,
    name: str = "Acme",
) -> Harness:
    charge_deps = deps or ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    with session_factory() as session:
        credentials = create_merchant(session, clock, name, TEST_API_KEY_PEPPER)
        session.commit()
    return Harness(charge_deps, credentials.merchant_id, {"Authorization": f"Bearer {credentials.api_key}"})


def _client(h: Harness) -> TestClient:
    return TestClient(create_app(GatewayAppDeps(charge=h.deps, api_key_pepper=TEST_API_KEY_PEPPER)))


def _request(pan: str = APPROVE_PAN, amount_minor: int = 1500, currency: str = "USD") -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
            "currency": currency,
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _charge(h: Harness, pan: str = APPROVE_PAN, amount_minor: int = 1500, currency: str = "USD") -> uuid.UUID:
    outcome = charge_card(h.deps, h.merchant_id, uuid.uuid4().hex, _request(pan, amount_minor, currency))
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["payment_id"]))


def _charge_series(h: Harness, clock: FixedClock, count: int) -> list[uuid.UUID]:
    """Creates `count` payments one minute apart; returns ids oldest first."""
    ids: list[uuid.UUID] = []
    for index in range(count):
        ids.append(_charge(h, amount_minor=1000 + index))
        clock.advance(timedelta(minutes=1))
    return ids


def _walk(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    limit: int,
    state: PaymentState | None = None,
) -> list[PaymentPage]:
    pages: list[PaymentPage] = []
    cursor: str | None = None
    for _ in range(100):
        with session_factory() as session:
            page = list_payments(session, merchant_id, limit=limit, cursor=cursor, state=state)
        pages.append(page)
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor
    raise AssertionError("pagination did not terminate")


def _set_totals(
    session_factory: sessionmaker[Session],
    payment_id: uuid.UUID,
    refunded: int,
    disputed: int,
    chargeback: int,
) -> None:
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE gateway.payments SET refunded_minor = :r, disputed_minor = :d, chargeback_minor = :c "
                "WHERE id = :id"
            ),
            {"r": refunded, "d": disputed, "c": chargeback, "id": payment_id},
        )
        session.commit()


# --- list_payments -------------------------------------------------------------------------------------


def test_list_payments_returns_the_merchants_payments_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 3)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    assert [item.payment_id for item in page.items] == list(reversed(ids))
    assert page.next_cursor is None


def test_list_payments_orders_payments_created_at_the_same_instant_by_id_descending(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = [_charge(h, amount_minor=1000 + index) for index in range(4)]

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    assert [item.payment_id for item in page.items] == sorted(ids, reverse=True)


def test_list_payments_excludes_other_merchants_payments(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    mine = _charge(h)
    _charge(other)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    assert [item.payment_id for item in page.items] == [mine]


def test_list_payments_for_a_merchant_without_payments_returns_an_empty_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    assert page == PaymentPage(items=[], next_cursor=None)


def test_list_payments_defaults_to_twenty_items_and_a_cursor_for_the_rest(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 21)

    # Act
    with session_factory() as session:
        first = list_payments(session, h.merchant_id)
        assert isinstance(first.next_cursor, str)
        second = list_payments(session, h.merchant_id, cursor=first.next_cursor)

    # Assert
    assert [item.payment_id for item in first.items] == list(reversed(ids))[:20]
    assert [item.payment_id for item in second.items] == [ids[0]]
    assert second.next_cursor is None


def test_list_payments_with_exactly_limit_rows_has_no_next_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 3)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, limit=3)

    # Assert
    assert [item.payment_id for item in page.items] == list(reversed(ids))
    assert page.next_cursor is None


def test_list_payments_walk_with_limit_two_returns_every_payment_exactly_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 5)

    # Act
    pages = _walk(session_factory, h.merchant_id, limit=2)

    # Assert
    assert [[item.payment_id for item in page.items] for page in pages] == [
        [ids[4], ids[3]],
        [ids[2], ids[1]],
        [ids[0]],
    ]
    assert [page.next_cursor is None for page in pages] == [False, False, True]


def test_list_payments_walk_does_not_skip_or_repeat_rows_sharing_a_timestamp(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    same_instant = [_charge(h, amount_minor=1000 + index) for index in range(5)]
    clock.advance(timedelta(minutes=1))
    newer = _charge(h, amount_minor=2000)

    # Act
    pages = _walk(session_factory, h.merchant_id, limit=2)

    # Assert
    walked = [item.payment_id for page in pages for item in page.items]
    assert walked == [newer, *sorted(same_instant, reverse=True)]


def test_list_payments_cursor_is_keyset_so_a_payment_created_between_pages_is_not_repeated(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 4)
    with session_factory() as session:
        first = list_payments(session, h.merchant_id, limit=2)
    assert isinstance(first.next_cursor, str)
    _charge(h, amount_minor=9999)

    # Act
    with session_factory() as session:
        second = list_payments(session, h.merchant_id, limit=2, cursor=first.next_cursor)

    # Assert
    assert [item.payment_id for item in first.items] == [ids[3], ids[2]]
    assert [item.payment_id for item in second.items] == [ids[1], ids[0]]


def test_list_payments_filters_by_state(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    approved = _charge(h)
    clock.advance(timedelta(minutes=1))
    declined = _charge(h, pan=DECLINED_PAN)

    # Act
    with session_factory() as session:
        only_declined = list_payments(session, h.merchant_id, state="declined")
        only_succeeded = list_payments(session, h.merchant_id, state="succeeded")
        only_failed = list_payments(session, h.merchant_id, state="failed")

    # Assert
    assert [item.payment_id for item in only_declined.items] == [declined]
    assert [item.payment_id for item in only_succeeded.items] == [approved]
    assert only_failed == PaymentPage(items=[], next_cursor=None)


def test_list_payments_state_filter_paginates_over_matching_rows_only(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    approved: list[uuid.UUID] = []
    for _ in range(3):
        approved.append(_charge(h))
        clock.advance(timedelta(minutes=1))
        _charge(h, pan=DECLINED_PAN)
        clock.advance(timedelta(minutes=1))

    # Act
    pages = _walk(session_factory, h.merchant_id, limit=2, state="succeeded")

    # Assert
    assert [[item.payment_id for item in page.items] for page in pages] == [
        [approved[2], approved[1]],
        [approved[0]],
    ]


def test_list_payments_summary_of_an_approved_card_payment_has_bin_last4_and_no_secrets(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=2500, currency="EUR")

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    expected = PaymentSummary(
        payment_id=payment_id,
        state="succeeded",
        amount_minor=2500,
        currency="EUR",
        method="card",
        card=CardSummary(bin="424242", last4="4242"),
        decline_code=None,
        refunded_minor=0,
        disputed_minor=0,
        chargeback_minor=0,
        created_at=clock(),
    )
    assert page.items == [expected]
    dumped = page.items[0].model_dump_json()
    assert APPROVE_PAN not in dumped
    assert card_fingerprint(APPROVE_PAN, TEST_CARD_FINGERPRINT_KEY) not in dumped


def test_list_payments_summary_of_a_declined_payment_carries_the_decline_code(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, pan=DECLINED_PAN)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    assert [(item.payment_id, item.state, item.decline_code) for item in page.items] == [
        (payment_id, "declined", "card_declined")
    ]


def test_list_payments_summary_reflects_the_payment_row_running_totals(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=1500)
    _set_totals(session_factory, payment_id, refunded=300, disputed=200, chargeback=100)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id)

    # Assert
    assert [(i.refunded_minor, i.disputed_minor, i.chargeback_minor) for i in page.items] == [(300, 200, 100)]


@pytest.mark.parametrize("cursor", ["not-a-cursor", "!!!", "bm90LWpzb24=", "e30="])
def test_list_payments_with_an_undecodable_cursor_raises_invalid_cursor_error(
    session_factory: sessionmaker[Session], clock: FixedClock, cursor: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)

    # Act / Assert
    with session_factory() as session, pytest.raises(InvalidCursorError):
        list_payments(session, h.merchant_id, cursor=cursor)


# --- GET /v1/payments ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_unknown"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_get_payments_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    client = _client(_harness(session_factory, clock))

    # Act
    response = client.get("/v1/payments", headers=headers)

    # Assert
    assert response.status_code == 401


def test_get_payments_returns_200_with_items_and_a_null_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 2)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", headers=h.auth)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert set(body) == {"items", "next_cursor"}
    assert body["next_cursor"] is None
    assert [PaymentSummary.model_validate(item).payment_id for item in body["items"]] == list(reversed(ids))
    assert body["items"][0] == {
        "payment_id": str(ids[1]),
        "state": "succeeded",
        "amount_minor": 1001,
        "currency": "USD",
        "method": "card",
        "card": {"bin": "424242", "last4": "4242"},
        "decline_code": None,
        "refunded_minor": 0,
        "disputed_minor": 0,
        "chargeback_minor": 0,
        "created_at": body["items"][0]["created_at"],
    }


def test_get_payments_only_lists_the_callers_payments(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    mine = _charge(h)
    _charge(other)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", headers=h.auth)

    # Assert
    assert [item["payment_id"] for item in response.json()["items"]] == [str(mine)]


def test_get_payments_follows_next_cursor_without_repeating_or_skipping(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    ids = _charge_series(h, clock, 5)
    client = _client(h)

    # Act
    first = client.get("/v1/payments", params={"limit": 2}, headers=h.auth).json()
    second = client.get(
        "/v1/payments", params={"limit": 2, "cursor": first["next_cursor"]}, headers=h.auth
    ).json()
    third = client.get(
        "/v1/payments", params={"limit": 2, "cursor": second["next_cursor"]}, headers=h.auth
    ).json()

    # Assert
    assert [item["payment_id"] for item in first["items"]] == [str(ids[4]), str(ids[3])]
    assert [item["payment_id"] for item in second["items"]] == [str(ids[2]), str(ids[1])]
    assert [item["payment_id"] for item in third["items"]] == [str(ids[0])]
    assert third["next_cursor"] is None


def test_get_payments_filters_by_state(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)
    clock.advance(timedelta(minutes=1))
    declined = _charge(h, pan=DECLINED_PAN)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"state": "declined"}, headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert [item["payment_id"] for item in response.json()["items"]] == [str(declined)]


@pytest.mark.parametrize("limit", ["0", "-1", "101", "abc"])
def test_get_payments_with_a_limit_outside_one_to_one_hundred_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock, limit: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"limit": limit}, headers=h.auth)

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize("limit", ["1", "100"])
def test_get_payments_accepts_the_limit_bounds(
    session_factory: sessionmaker[Session], clock: FixedClock, limit: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"limit": limit}, headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert [item["payment_id"] for item in response.json()["items"]] == [str(payment_id)]


def test_get_payments_with_an_unknown_state_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"state": "refunded"}, headers=h.auth)

    # Assert
    assert response.status_code == 422


def test_get_payments_with_an_invalid_cursor_returns_400_invalid_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"cursor": "not-a-cursor"}, headers=h.auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": "invalid cursor"}


# --- get_payment ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pan", "expected_state"),
    [(APPROVE_PAN, "succeeded"), (DECLINED_PAN, "declined")],
    ids=["approved_card_payment", "declined_payment"],
)
def test_get_payment_returns_the_same_summary_as_the_matching_list_payments_item(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str, expected_state: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, pan=pan, amount_minor=2500, currency="EUR")

    # Act
    with session_factory() as session:
        summary = get_payment(session, h.merchant_id, payment_id)
        listed = list_payments(session, h.merchant_id).items

    # Assert
    assert listed == [summary]
    assert (summary.payment_id, summary.state) == (payment_id, expected_state)


def test_get_payment_of_an_approved_card_payment_carries_bin_and_last4(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=2500, currency="EUR")

    # Act
    with session_factory() as session:
        summary = get_payment(session, h.merchant_id, payment_id)

    # Assert
    assert summary == PaymentSummary(
        payment_id=payment_id,
        state="succeeded",
        amount_minor=2500,
        currency="EUR",
        method="card",
        card=CardSummary(bin="424242", last4="4242"),
        decline_code=None,
        refunded_minor=0,
        disputed_minor=0,
        chargeback_minor=0,
        created_at=clock(),
    )


def test_get_payment_of_another_merchants_payment_raises_payment_not_found_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    others_payment = _charge(other)

    # Act / Assert
    with session_factory() as session, pytest.raises(PaymentNotFoundError):
        get_payment(session, h.merchant_id, others_payment)


def test_get_payment_with_an_unknown_id_raises_payment_not_found_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)

    # Act / Assert
    with session_factory() as session, pytest.raises(PaymentNotFoundError):
        get_payment(session, h.merchant_id, uuid.uuid4())


# --- GET /v1/payments/{payment_id} ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_unknown"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_get_payment_by_id_without_a_valid_api_key_returns_401(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h)
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}", headers=headers)

    # Assert
    assert response.status_code == 401


@pytest.mark.parametrize("pan", [APPROVE_PAN, DECLINED_PAN], ids=["approved", "declined"])
def test_get_payment_by_id_returns_200_equal_to_the_list_item_with_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, pan=pan)
    client = _client(h)
    listed = client.get("/v1/payments", headers=h.auth).json()["items"]

    # Act
    response = client.get(f"/v1/payments/{payment_id}", headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert response.json() == listed[0]
    assert response.json()["payment_id"] == str(payment_id)
    assert response.headers["Cache-Control"] == "no-store"


def test_get_payment_by_id_for_an_approved_card_payment_returns_the_full_summary_json(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    payment_id = _charge(h, amount_minor=2500, currency="EUR")
    client = _client(h)

    # Act
    response = client.get(f"/v1/payments/{payment_id}", headers=h.auth)

    # Assert
    body = response.json()
    assert response.status_code == 200
    assert body == {
        "payment_id": str(payment_id),
        "state": "succeeded",
        "amount_minor": 2500,
        "currency": "EUR",
        "method": "card",
        "card": {"bin": "424242", "last4": "4242"},
        "decline_code": None,
        "refunded_minor": 0,
        "disputed_minor": 0,
        "chargeback_minor": 0,
        "created_at": body["created_at"],
    }


def test_get_payment_by_id_returns_identical_404_for_another_merchants_and_an_unknown_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    others_payment = _charge(other)
    client = _client(h)

    # Act
    foreign = client.get(f"/v1/payments/{others_payment}", headers=h.auth)
    unknown = client.get(f"/v1/payments/{uuid.uuid4()}", headers=h.auth)

    # Assert
    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert foreign.json() == {"detail": "payment not found"}
    assert unknown.json() == foreign.json()
    assert foreign.headers["Cache-Control"] == "no-store"
    assert unknown.headers["Cache-Control"] == "no-store"


def test_get_payment_by_id_with_a_non_uuid_id_returns_422(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    client = _client(h)

    # Act
    response = client.get("/v1/payments/not-a-uuid", headers=h.auth)

    # Assert
    assert response.status_code == 422


def test_get_payment_by_id_404s_for_another_merchants_and_an_unknown_payment_have_identical_headers(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    others_payment = _charge(other)
    client = _client(h)

    # Act
    foreign = client.get(f"/v1/payments/{others_payment}", headers=h.auth)
    unknown = client.get(f"/v1/payments/{uuid.uuid4()}", headers=h.auth)

    # Assert
    assert (foreign.status_code, unknown.status_code) == (404, 404)
    assert dict(foreign.headers) == dict(unknown.headers)
    assert foreign.headers["Pragma"] == "no-cache"
    assert unknown.headers["Pragma"] == "no-cache"


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer sk_test_unknown"}, {"Authorization": "Basic c2tfdGVzdF94Og=="}],
    ids=["no_auth", "unknown_bearer_key", "not_a_bearer_scheme"],
)
def test_get_payment_by_id_with_a_non_uuid_id_and_no_valid_api_key_returns_401_not_422(
    session_factory: sessionmaker[Session], clock: FixedClock, headers: dict[str, str]
) -> None:
    # Arrange
    client = _client(_harness(session_factory, clock))

    # Act
    response = client.get("/v1/payments/not-a-uuid", headers=headers)

    # Assert
    assert response.status_code == 401


# --- B3.1 filters: created_from / created_to / method --------------------------------------------------

T0 = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)
JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class Timeline:
    """Five payments around the half-open range [T0, T0 + 3h)."""

    before: uuid.UUID  # T0 - 1s, succeeded
    at_from: uuid.UUID  # T0, succeeded
    mid: uuid.UUID  # T0 + 1h, declined
    late: uuid.UUID  # T0 + 2h, succeeded
    at_to: uuid.UUID  # T0 + 3h, succeeded

    @property
    def to(self) -> datetime:
        return T0 + timedelta(hours=3)


def _charge_at(h: Harness, clock: FixedClock, instant: datetime, pan: str = APPROVE_PAN) -> uuid.UUID:
    clock.advance(instant - clock())
    return _charge(h, pan=pan)


def _timeline(h: Harness, clock: FixedClock) -> Timeline:
    return Timeline(
        before=_charge_at(h, clock, T0 - timedelta(seconds=1)),
        at_from=_charge_at(h, clock, T0),
        mid=_charge_at(h, clock, T0 + timedelta(hours=1), pan=DECLINED_PAN),
        late=_charge_at(h, clock, T0 + timedelta(hours=2)),
        at_to=_charge_at(h, clock, T0 + timedelta(hours=3)),
    )


def _ids(page: PaymentPage) -> list[uuid.UUID]:
    return [item.payment_id for item in page.items]


def _body_ids(body: dict[str, object]) -> list[str]:
    items = body["items"]
    assert isinstance(items, list)
    return [str(item["payment_id"]) for item in items]


def test_payment_methods_is_the_frozenset_containing_only_card() -> None:
    # Arrange / Act
    methods = PAYMENT_METHODS

    # Assert
    assert methods == frozenset({"card"})
    assert isinstance(methods, frozenset)


def test_list_payments_created_from_is_inclusive_and_excludes_an_earlier_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, created_from=T0)

    # Assert
    assert _ids(page) == [t.at_to, t.late, t.mid, t.at_from]


def test_list_payments_created_to_is_exclusive(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, created_to=t.to)

    # Assert
    assert _ids(page) == [t.late, t.mid, t.at_from, t.before]


def test_list_payments_half_open_range_includes_from_and_excludes_to_and_earlier(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, created_from=T0, created_to=t.to)

    # Assert
    assert _ids(page) == [t.late, t.mid, t.at_from]
    assert page.next_cursor is None


def test_list_payments_range_starting_just_after_from_excludes_the_payment_at_from(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(
            session, h.merchant_id, created_from=T0 + timedelta(microseconds=1), created_to=t.to
        )

    # Assert
    assert _ids(page) == [t.late, t.mid]


def test_list_payments_range_matching_nothing_returns_an_empty_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(
            session,
            h.merchant_id,
            created_from=T0 + timedelta(days=1),
            created_to=T0 + timedelta(days=2),
        )

    # Assert
    assert page == PaymentPage(items=[], next_cursor=None)


def test_list_payments_converts_a_non_utc_aware_bound_to_utc(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    from_jst = datetime(2026, 11, 4, 9, 0, tzinfo=JST)  # == T0
    to_jst = datetime(2026, 11, 4, 12, 0, tzinfo=JST)  # == T0 + 3h

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, created_from=from_jst, created_to=to_jst)

    # Assert
    assert _ids(page) == [t.late, t.mid, t.at_from]


@pytest.mark.parametrize(
    ("created_from", "created_to"),
    [
        (datetime(2026, 11, 4, 0, 0), None),
        (None, datetime(2026, 11, 4, 3, 0)),
        (datetime(2026, 11, 4, 0, 0), T0 + timedelta(hours=3)),
        (T0, datetime(2026, 11, 4, 3, 0)),
    ],
    ids=["naive_from", "naive_to", "naive_from_aware_to", "aware_from_naive_to"],
)
def test_list_payments_with_a_naive_datetime_raises_value_error(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    created_from: datetime | None,
    created_to: datetime | None,
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)

    # Act / Assert
    with session_factory() as session, pytest.raises(ValueError):
        list_payments(session, h.merchant_id, created_from=created_from, created_to=created_to)


def test_list_payments_method_card_returns_all_card_payments(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, method="card")

    # Assert
    assert _ids(page) == [t.at_to, t.late, t.mid, t.at_from, t.before]


def test_list_payments_method_card_combines_with_the_date_range(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        page = list_payments(session, h.merchant_id, method="card", created_from=T0, created_to=t.to)

    # Assert
    assert _ids(page) == [t.late, t.mid, t.at_from]


def test_list_payments_state_and_date_range_combine_with_and(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)

    # Act
    with session_factory() as session:
        declined = list_payments(session, h.merchant_id, state="declined", created_from=T0, created_to=t.to)
        succeeded = list_payments(session, h.merchant_id, state="succeeded", created_from=T0, created_to=t.to)
        declined_outside = list_payments(session, h.merchant_id, state="declined", created_from=t.to)

    # Assert
    assert _ids(declined) == [t.mid]
    assert _ids(succeeded) == [t.late, t.at_from]
    assert declined_outside == PaymentPage(items=[], next_cursor=None)


def test_list_payments_date_filter_does_not_leak_another_merchants_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    mine = _charge_at(h, clock, T0 + timedelta(hours=1))
    _charge_at(other, clock, T0 + timedelta(hours=1))
    _charge_at(other, clock, T0 + timedelta(hours=2))

    # Act
    with session_factory() as session:
        page = list_payments(
            session, h.merchant_id, created_from=T0, created_to=T0 + timedelta(hours=3), method="card"
        )

    # Assert
    assert _ids(page) == [mine]


def _tie_series(h: Harness, clock: FixedClock) -> tuple[list[uuid.UUID], datetime, datetime]:
    """Seven payments around [T0, T0 + 40m); returns the in-range ids newest first, from and to."""
    _charge_at(h, clock, T0 - timedelta(minutes=10))
    tied = [_charge_at(h, clock, T0), _charge_at(h, clock, T0)]
    in_range = [_charge_at(h, clock, T0 + timedelta(minutes=minutes)) for minutes in (10, 20, 30)]
    _charge_at(h, clock, T0 + timedelta(minutes=40))
    expected = [*reversed(in_range), *sorted(tied, reverse=True)]
    return expected, T0, T0 + timedelta(minutes=40)


def test_list_payments_walk_under_a_date_filter_returns_exactly_the_matching_rows_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    expected, created_from, created_to = _tie_series(h, clock)
    pages: list[PaymentPage] = []
    cursor: str | None = None

    # Act
    for _ in range(100):
        with session_factory() as session:
            page = list_payments(
                session,
                h.merchant_id,
                limit=2,
                cursor=cursor,
                created_from=created_from,
                created_to=created_to,
            )
        pages.append(page)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    # Assert
    assert [_ids(page) for page in pages] == [expected[0:2], expected[2:4], expected[4:5]]
    assert [page.next_cursor is None for page in pages] == [False, False, True]


def test_list_payments_cursor_outside_the_date_range_yields_an_empty_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    with session_factory() as session:
        newest = list_payments(session, h.merchant_id, limit=1)
    assert isinstance(newest.next_cursor, str)
    assert _ids(newest) == [t.at_to]

    # Act
    with session_factory() as session:
        page = list_payments(
            session,
            h.merchant_id,
            cursor=newest.next_cursor,
            created_from=t.to,
            created_to=t.to + timedelta(hours=1),
        )

    # Assert
    assert page == PaymentPage(items=[], next_cursor=None)


# --- GET /v1/payments filters ---------------------------------------------------------------------------


def test_get_payments_created_from_and_created_to_apply_the_half_open_range(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    response = client.get(
        "/v1/payments",
        params={"created_from": T0.isoformat(), "created_to": t.to.isoformat()},
        headers=h.auth,
    )

    # Assert
    assert response.status_code == 200
    assert _body_ids(response.json()) == [str(t.late), str(t.mid), str(t.at_from)]
    assert response.json()["next_cursor"] is None


def test_get_payments_accepts_a_z_suffix_and_each_bound_alone(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    only_from = client.get("/v1/payments", params={"created_from": "2026-11-04T00:00:00Z"}, headers=h.auth)
    only_to = client.get("/v1/payments", params={"created_to": "2026-11-04T03:00:00Z"}, headers=h.auth)

    # Assert
    assert (only_from.status_code, only_to.status_code) == (200, 200)
    assert _body_ids(only_from.json()) == [str(t.at_to), str(t.late), str(t.mid), str(t.at_from)]
    assert _body_ids(only_to.json()) == [str(t.late), str(t.mid), str(t.at_from), str(t.before)]


def test_get_payments_honors_the_offset_so_plus_nine_equals_utc(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    utc = client.get(
        "/v1/payments",
        params={"created_from": "2026-11-04T00:00:00Z", "created_to": "2026-11-04T03:00:00Z"},
        headers=h.auth,
    )
    jst = client.get(
        "/v1/payments",
        params={"created_from": "2026-11-04T09:00:00+09:00", "created_to": "2026-11-04T12:00:00+09:00"},
        headers=h.auth,
    )

    # Assert
    assert (utc.status_code, jst.status_code) == (200, 200)
    assert _body_ids(jst.json()) == [str(t.late), str(t.mid), str(t.at_from)]
    assert jst.json() == utc.json()


def test_get_payments_accepts_a_url_encoded_plus_offset_in_the_raw_query_string(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    response = client.get(
        "/v1/payments?created_from=2026-11-04T09:00:00%2B09:00&created_to=2026-11-04T12:00:00%2B09:00",
        headers=h.auth,
    )

    # Assert
    assert response.status_code == 200
    assert _body_ids(response.json()) == [str(t.late), str(t.mid), str(t.at_from)]


def test_get_payments_method_card_returns_the_card_payments(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params={"method": "card"}, headers=h.auth)

    # Assert
    assert response.status_code == 200
    assert _body_ids(response.json()) == [
        str(t.at_to),
        str(t.late),
        str(t.mid),
        str(t.at_from),
        str(t.before),
    ]


def test_get_payments_state_method_and_date_range_combine_with_and(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    response = client.get(
        "/v1/payments",
        params={
            "state": "declined",
            "method": "card",
            "created_from": T0.isoformat(),
            "created_to": t.to.isoformat(),
        },
        headers=h.auth,
    )

    # Assert
    assert response.status_code == 200
    assert _body_ids(response.json()) == [str(t.mid)]


def test_get_payments_date_filter_does_not_leak_another_merchants_payment(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    other = _harness(session_factory, clock, deps=h.deps, name="Other")
    mine = _charge_at(h, clock, T0 + timedelta(hours=1))
    _charge_at(other, clock, T0 + timedelta(hours=1))
    client = _client(h)

    # Act
    response = client.get(
        "/v1/payments",
        params={"created_from": T0.isoformat(), "created_to": (T0 + timedelta(hours=3)).isoformat()},
        headers=h.auth,
    )

    # Assert
    assert response.status_code == 200
    assert _body_ids(response.json()) == [str(mine)]


def test_get_payments_walk_with_limit_two_under_a_date_filter_returns_the_matching_rows_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    expected, created_from, created_to = _tie_series(h, clock)
    client = _client(h)
    filters = {"created_from": created_from.isoformat(), "created_to": created_to.isoformat(), "limit": "2"}
    pages: list[dict[str, object]] = []
    cursor: str | None = None

    # Act
    for _ in range(100):
        params = dict(filters)
        if cursor is not None:
            params["cursor"] = cursor
        body: dict[str, object] = client.get("/v1/payments", params=params, headers=h.auth).json()
        pages.append(body)
        next_cursor = body["next_cursor"]
        if next_cursor is None:
            break
        assert isinstance(next_cursor, str)
        cursor = next_cursor

    # Assert
    assert [_body_ids(page) for page in pages] == [
        [str(i) for i in expected[0:2]],
        [str(i) for i in expected[2:4]],
        [str(i) for i in expected[4:5]],
    ]
    assert [page["next_cursor"] is None for page in pages] == [False, False, True]


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"created_from": "yesterday"}, "invalid created_from"),
        ({"created_from": "2026-11-04T00:00:00"}, "invalid created_from"),
        ({"created_from": "2026-11-04"}, "invalid created_from"),
        ({"created_to": "yesterday"}, "invalid created_to"),
        ({"created_to": "2026-11-04T00:00:00"}, "invalid created_to"),
        ({"created_from": "yesterday", "created_to": "2026-11-04T03:00:00Z"}, "invalid created_from"),
        ({"created_from": "2026-11-04T00:00:00Z", "created_to": "2026-11-04T03:00:00"}, "invalid created_to"),
        (
            {"created_from": "2026-11-04T00:00:00Z", "created_to": "2026-11-04T00:00:00Z"},
            "invalid date range",
        ),
        (
            {"created_from": "2026-11-04T03:00:00Z", "created_to": "2026-11-04T00:00:00Z"},
            "invalid date range",
        ),
        (
            {"created_from": "2026-11-04T09:00:00+09:00", "created_to": "2026-11-04T00:00:00Z"},
            "invalid date range",
        ),
        ({"method": "wire"}, "invalid method"),
        ({"method": "CARD"}, "invalid method"),
    ],
    ids=[
        "from_garbage",
        "from_naive",
        "from_date_only",
        "to_garbage",
        "to_naive",
        "from_garbage_with_valid_to",
        "to_naive_with_valid_from",
        "range_from_equals_to",
        "range_from_after_to",
        "range_equal_instant_across_offsets",
        "method_wire",
        "method_wrong_case",
    ],
)
def test_get_payments_with_an_invalid_filter_returns_400_and_the_exact_detail(
    session_factory: sessionmaker[Session], clock: FixedClock, params: dict[str, str], detail: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params=params, headers=h.auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize(
    ("params", "detail"),
    [
        ({"created_from": "9999-12-31T23:59:59-23:59"}, "invalid created_from"),
        ({"created_to": "0001-01-01T00:00:00+23:59"}, "invalid created_to"),
    ],
    ids=["from_unrepresentable_in_utc_after_max", "to_unrepresentable_in_utc_before_min"],
)
def test_get_payments_with_a_bound_unrepresentable_in_utc_returns_400_and_the_exact_detail(
    session_factory: sessionmaker[Session], clock: FixedClock, params: dict[str, str], detail: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    _charge(h)
    client = _client(h)

    # Act
    response = client.get("/v1/payments", params=params, headers=h.auth)

    # Assert
    assert response.status_code == 400
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize(
    ("created_from", "created_to"),
    [
        ("2026-11-04T23:00:00+23:00", "2026-11-05T02:00:00+23:00"),
        ("2026-11-04T23:59:00+23:59", "2026-11-05T02:59:00+23:59"),
        ("2026-11-03T01:00:00-23:00", "2026-11-03T04:00:00-23:00"),
    ],
    ids=["plus_23_00", "plus_23_59", "minus_23_00"],
)
def test_get_payments_accepts_a_large_offset_and_filters_like_the_equivalent_utc_range(
    session_factory: sessionmaker[Session], clock: FixedClock, created_from: str, created_to: str
) -> None:
    # Arrange
    h = _harness(session_factory, clock)
    t = _timeline(h, clock)
    client = _client(h)

    # Act
    utc = client.get(
        "/v1/payments",
        params={"created_from": "2026-11-04T00:00:00Z", "created_to": "2026-11-04T03:00:00Z"},
        headers=h.auth,
    )
    offset = client.get(
        "/v1/payments",
        params={"created_from": created_from, "created_to": created_to},
        headers=h.auth,
    )

    # Assert
    assert (utc.status_code, offset.status_code) == (200, 200)
    assert _body_ids(utc.json()) == [str(t.late), str(t.mid), str(t.at_from)]
    assert _body_ids(offset.json()) == [str(t.late), str(t.mid), str(t.at_from)]
    assert offset.json() == utc.json()
