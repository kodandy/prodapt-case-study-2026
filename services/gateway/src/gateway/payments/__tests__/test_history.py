"""RED tests for B3 history, written from docs/specs/gateway-basic.md (history.py + GET /v1/payments)."""

import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from gateway.accounts.service import create_merchant
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.history import InvalidCursorError, PaymentPage, list_payments
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
