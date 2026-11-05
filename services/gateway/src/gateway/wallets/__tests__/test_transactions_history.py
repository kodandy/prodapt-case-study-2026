"""Tests for `list_transactions` and GET /v1/me/transactions (consumer transactions, B3.1)."""

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from gateway.accounts.service import sign_up
from gateway.accounts.sessions import issue_session_token
from gateway.app import GatewayAppDeps, create_app
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps
from gateway.payments.history import InvalidCursorError
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.wallets.history import (
    TRANSACTION_METHODS,
    TRANSACTION_STATUSES,
    TransactionItem,
    TransactionPage,
    list_transactions,
)
from gateway.wallets.models import Transfer
from gateway.wallets.service import top_up_wallet, transfer
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY, TEST_NOW

TRANSACTIONS = "/v1/me/transactions"
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
DAVE = "dave@example.com"
ALICE_MASKED = "a***@example.com"
BOB_MASKED = "b***@example.com"
CAROL_MASKED = "c***@example.com"
PASSWORD = "transactions-test-password-7Q"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
DEFAULT_PAGE_SIZE = 20
METHOD = "p2p_transfer"
STATUS = "succeeded"
MISSING_SESSION = {"detail": "missing session"}
INVALID_SESSION = {"detail": "invalid session"}
NO_STORE = "no-store"
VALID_CURSOR_ID = "00000000-0000-0000-0000-000000000001"
JST = timezone(timedelta(hours=9))


def _cursor(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode()


INVALID_CURSORS = {
    "garbage": "not-a-cursor",
    "not_utf8": base64.urlsafe_b64encode(b"\xff\xfe|\xfd").decode(),
    "non_utc_offset": _cursor(f"2026-11-03T12:00:00+09:00|{VALID_CURSOR_ID}"),
    "naive_timestamp": _cursor(f"2026-11-03T12:00:00|{VALID_CURSOR_ID}"),
    "too_few_parts": _cursor("2026-11-03T12:00:00+00:00"),
    "bad_uuid": _cursor("2026-11-03T12:00:00+00:00|not-a-uuid"),
}


@dataclass(frozen=True)
class Party:
    user_id: uuid.UUID
    account_id: uuid.UUID


def _generate_private_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


@pytest.fixture(scope="module")
def private_pem() -> str:
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


def _sign_up(session_factory: sessionmaker[Session], clock: FixedClock, email: str) -> Party:
    with session_factory() as session:
        result = sign_up(session, clock, email, PASSWORD)
        session.commit()
    return Party(user_id=result.user_id, account_id=result.wallet_account_id)


def _fund(session_factory: sessionmaker[Session], clock: FixedClock, party: Party, amount_minor: int) -> None:
    with session_factory.begin() as session:
        top_up_wallet(session, clock, party.user_id, "USD", amount_minor)


def _send(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    sender: Party,
    recipient_email: str,
    amount_minor: int,
) -> uuid.UUID:
    """A real transfer through the service; returns its id."""
    outcome = transfer(
        session_factory,
        clock,
        sender_user_id=sender.user_id,
        idempotency_key=f"k-{uuid.uuid4()}",
        recipient_email=recipient_email,
        amount_minor=amount_minor,
        currency="USD",
    )
    assert outcome.status_code == 201
    return uuid.UUID(str(outcome.body["transfer_id"]))


def _at(minutes: int) -> datetime:
    return TEST_NOW + timedelta(minutes=minutes)


def _row(
    sender: Party,
    recipient: Party,
    created_at: datetime,
    transfer_id: uuid.UUID | None = None,
    amount_minor: int = 100,
    currency: str = "USD",
) -> Transfer:
    row_id = uuid.uuid4() if transfer_id is None else transfer_id
    return Transfer(
        id=row_id,
        sender_user_id=sender.user_id,
        recipient_user_id=recipient.user_id,
        sender_account_id=sender.account_id,
        recipient_account_id=recipient.account_id,
        amount_minor=amount_minor,
        currency=currency,
        state="succeeded",
        idempotency_key=f"k-{row_id}",
        request_hash="0" * 64,
        response_body={"state": "succeeded"},
        created_at=created_at,
    )


def _store(session_factory: sessionmaker[Session], rows: Sequence[Transfer]) -> list[uuid.UUID]:
    with session_factory.begin() as session:
        session.add_all(rows)
    return [row.id for row in rows]


def _spread(
    session_factory: sessionmaker[Session], sender: Party, recipient: Party, count: int
) -> list[uuid.UUID]:
    """`count` transfers one minute apart (minute 0 .. count-1); returns the ids newest first."""
    ids = _store(session_factory, [_row(sender, recipient, _at(minute)) for minute in range(count)])
    return list(reversed(ids))


def _list(
    session_factory: sessionmaker[Session],
    party: Party,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
    status: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    method: str | None = None,
) -> TransactionPage:
    with session_factory() as session:
        return list_transactions(
            session,
            party.user_id,
            limit,
            cursor,
            status=status,
            created_from=created_from,
            created_to=created_to,
            method=method,
        )


def _list_with_defaults(session_factory: sessionmaker[Session], party: Party) -> TransactionPage:
    """Calls `list_transactions` with only the required arguments, to pin the signature's defaults."""
    with session_factory() as session:
        return list_transactions(session, party.user_id)


def _ids(page: TransactionPage) -> list[uuid.UUID]:
    return [item.transaction_id for item in page.items]


def _walk_service(
    session_factory: sessionmaker[Session],
    party: Party,
    limit: int,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> tuple[list[list[uuid.UUID]], str | None]:
    """Follows next_cursor to the end; returns the ids per page and the last page's next_cursor."""
    pages: list[list[uuid.UUID]] = []
    cursor: str | None = None
    while True:
        page = _list(session_factory, party, limit, cursor, created_from=created_from, created_to=created_to)
        pages.append(_ids(page))
        if page.next_cursor is None:
            return pages, page.next_cursor
        cursor = page.next_cursor


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(private_pem: str, clock: FixedClock, party: Party) -> str:
    return issue_session_token(private_pem, party.user_id, clock(), ("pwd",))


def _get(client: TestClient, token: str | None, params: dict[str, str | int] | None = None) -> Response:
    headers = {} if token is None else _bearer(token)
    response: Response = client.get(TRANSACTIONS, headers=headers, params=params)
    return response


def _items(response: Response) -> list[dict[str, object]]:
    items: list[dict[str, object]] = response.json()["items"]
    return items


def _item_ids(response: Response) -> list[str]:
    return [str(item["transaction_id"]) for item in _items(response)]


def _next_cursor(response: Response) -> str | None:
    body: dict[str, object] = response.json()
    cursor = body["next_cursor"]
    return None if cursor is None else str(cursor)


def _walk_http(
    client: TestClient, token: str, limit: int, extra: dict[str, str | int] | None = None
) -> tuple[list[list[str]], str | None]:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": limit}
        if extra is not None:
            params.update(extra)
        if cursor is not None:
            params["cursor"] = cursor
        response = _get(client, token, params)
        assert response.status_code == 200
        pages.append(_item_ids(response))
        cursor = _next_cursor(response)
        if cursor is None:
            return pages, cursor


# --- constants --------------------------------------------------------------------------------------------


def test_transaction_filter_vocabularies_hold_only_p2p_transfer_and_succeeded() -> None:
    # Arrange
    # (module constants)

    # Act
    methods = TRANSACTION_METHODS
    statuses = TRANSACTION_STATUSES

    # Assert
    assert methods == frozenset({"p2p_transfer"})
    assert statuses == frozenset({"succeeded"})


# --- list_transactions: scoping, direction and shape ------------------------------------------------------


def test_list_transactions_for_a_user_without_transfers_returns_an_empty_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    _store(session_factory, [_row(bob, carol, _at(0))])

    # Act
    page = _list(session_factory, alice)

    # Assert
    assert page == TransactionPage(items=[], next_cursor=None)


def test_list_transactions_returns_only_transfers_where_the_user_is_sender_or_recipient(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    dave = _sign_up(session_factory, clock, DAVE)
    a_to_b, b_to_c, c_to_a = _store(
        session_factory,
        [_row(alice, bob, _at(0)), _row(bob, carol, _at(1)), _row(carol, alice, _at(2))],
    )

    # Act
    alice_page = _list(session_factory, alice)
    bob_page = _list(session_factory, bob)
    dave_page = _list(session_factory, dave)

    # Assert
    assert _ids(alice_page) == [c_to_a, a_to_b]
    assert _ids(bob_page) == [b_to_c, a_to_b]
    assert dave_page == TransactionPage(items=[], next_cursor=None)


def test_list_transactions_shows_a_sent_item_to_the_sender_and_a_received_item_to_the_recipient(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    transaction_id = _send(session_factory, clock, alice, BOB, 2_500)

    # Act
    alice_page = _list(session_factory, alice)
    bob_page = _list(session_factory, bob)

    # Assert
    assert alice_page == TransactionPage(
        items=[
            TransactionItem(
                transaction_id=transaction_id,
                method="p2p_transfer",
                direction="sent",
                status="succeeded",
                counterparty_email_masked=BOB_MASKED,
                amount_minor=2_500,
                currency="USD",
                created_at=clock(),
            )
        ],
        next_cursor=None,
    )
    assert bob_page == TransactionPage(
        items=[
            TransactionItem(
                transaction_id=transaction_id,
                method="p2p_transfer",
                direction="received",
                status="succeeded",
                counterparty_email_masked=ALICE_MASKED,
                amount_minor=2_500,
                currency="USD",
                created_at=clock(),
            )
        ],
        next_cursor=None,
    )


def test_list_transactions_reports_each_amount_and_currency_of_a_users_mixed_history(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    sent, received = _store(
        session_factory,
        [
            _row(alice, bob, _at(0), amount_minor=1_500, currency="JPY"),
            _row(carol, alice, _at(1), amount_minor=7, currency="EUR"),
        ],
    )

    # Act
    page = _list(session_factory, alice)

    # Assert
    assert page.items == [
        TransactionItem(
            transaction_id=received,
            method="p2p_transfer",
            direction="received",
            status="succeeded",
            counterparty_email_masked=CAROL_MASKED,
            amount_minor=7,
            currency="EUR",
            created_at=_at(1),
        ),
        TransactionItem(
            transaction_id=sent,
            method="p2p_transfer",
            direction="sent",
            status="succeeded",
            counterparty_email_masked=BOB_MASKED,
            amount_minor=1_500,
            currency="JPY",
            created_at=_at(0),
        ),
    ]


# --- list_transactions: ordering and paging ---------------------------------------------------------------


def test_list_transactions_returns_newest_first_across_both_directions(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    oldest, middle, newest = _store(
        session_factory,
        [_row(alice, bob, _at(0)), _row(bob, alice, _at(1)), _row(alice, bob, _at(2))],
    )

    # Act
    page = _list(session_factory, alice)

    # Assert
    assert _ids(page) == [newest, middle, oldest]
    assert [item.direction for item in page.items] == ["sent", "received", "sent"]


def test_list_transactions_breaks_a_created_at_tie_by_id_descending(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    low, middle, high, newer = (uuid.UUID(int=n) for n in (1, 2, 3, 0))
    _store(
        session_factory,
        [
            _row(alice, bob, _at(5), transfer_id=middle),
            _row(bob, alice, _at(5), transfer_id=high),
            _row(alice, bob, _at(5), transfer_id=low),
            _row(alice, bob, _at(6), transfer_id=newer),
        ],
    )

    # Act
    page = _list(session_factory, alice)

    # Assert
    assert _ids(page) == [newer, high, middle, low]


def test_list_transactions_limit_truncates_the_page_and_chained_cursors_cover_every_row_once(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 5)

    # Act
    first_page = _list(session_factory, alice, limit=2)
    pages, last_cursor = _walk_service(session_factory, alice, limit=2)

    # Assert
    assert _ids(first_page) == expected[:2]
    assert pages == [expected[:2], expected[2:4], expected[4:]]
    assert last_cursor is None


def test_list_transactions_pages_through_a_created_at_tie_without_duplicates_or_gaps(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    ids = [uuid.UUID(int=n) for n in (1, 2, 3, 4)]
    _store(session_factory, [_row(alice, bob, _at(3), transfer_id=row_id) for row_id in ids])

    # Act
    pages, last_cursor = _walk_service(session_factory, alice, limit=1)

    # Assert
    assert pages == [[ids[3]], [ids[2]], [ids[1]], [ids[0]]]
    assert last_cursor is None


def test_list_transactions_has_no_next_cursor_when_the_rows_exactly_fill_the_limit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 3)

    # Act
    page = _list(session_factory, alice, limit=3)

    # Assert
    assert _ids(page) == expected
    assert page.next_cursor is None


def test_list_transactions_defaults_to_twenty_items_per_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, DEFAULT_PAGE_SIZE + 1)

    # Act
    first = _list_with_defaults(session_factory, alice)
    assert first.next_cursor is not None
    second = _list(session_factory, alice, cursor=first.next_cursor)

    # Assert
    assert _ids(first) == expected[:DEFAULT_PAGE_SIZE]
    assert _ids(second) == expected[DEFAULT_PAGE_SIZE:]
    assert second.next_cursor is None


def test_list_transactions_cursor_never_returns_rows_of_another_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    alice_rows = _spread(session_factory, alice, bob, 3)
    _store(session_factory, [_row(bob, carol, _at(minute)) for minute in range(10, 13)])
    first = _list(session_factory, alice, limit=1)
    assert first.next_cursor is not None

    # Act
    rest = _list(session_factory, alice, limit=10, cursor=first.next_cursor)

    # Assert
    assert _ids(first) + _ids(rest) == alice_rows


# --- list_transactions: filters ---------------------------------------------------------------------------


def test_list_transactions_status_succeeded_keeps_every_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 3)

    # Act
    page = _list(session_factory, alice, status="succeeded")

    # Assert
    assert _ids(page) == expected


def test_list_transactions_method_p2p_transfer_keeps_every_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 3)

    # Act
    page = _list(session_factory, alice, method="p2p_transfer")

    # Assert
    assert _ids(page) == expected


def test_list_transactions_created_from_is_inclusive(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = _spread(session_factory, alice, bob, 4)

    # Act
    page = _list(session_factory, alice, created_from=_at(1))

    # Assert
    assert _ids(page) == newest_first[:3]


def test_list_transactions_created_to_is_exclusive(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = _spread(session_factory, alice, bob, 4)

    # Act
    page = _list(session_factory, alice, created_to=_at(3))

    # Assert
    assert _ids(page) == newest_first[1:]


def test_list_transactions_date_range_is_half_open(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = _spread(session_factory, alice, bob, 5)

    # Act
    page = _list(session_factory, alice, created_from=_at(1), created_to=_at(4))

    # Assert
    assert _ids(page) == newest_first[1:4]


def test_list_transactions_date_range_matching_no_row_returns_an_empty_page(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 3)

    # Act
    page = _list(session_factory, alice, created_from=_at(10), created_to=_at(20))

    # Assert
    assert page == TransactionPage(items=[], next_cursor=None)


def test_list_transactions_compares_non_utc_aware_bounds_as_instants(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = _spread(session_factory, alice, bob, 5)

    # Act
    page = _list(
        session_factory,
        alice,
        created_from=_at(1).astimezone(JST),
        created_to=_at(4).astimezone(JST),
    )

    # Assert
    assert _ids(page) == newest_first[1:4]


def test_list_transactions_date_filter_applies_to_the_received_direction_too(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _, inside_received, inside_sent, _ = _store(
        session_factory,
        [
            _row(alice, bob, _at(0)),
            _row(bob, alice, _at(1)),
            _row(alice, bob, _at(2)),
            _row(bob, alice, _at(3)),
        ],
    )

    # Act
    page = _list(session_factory, alice, created_from=_at(1), created_to=_at(3))

    # Assert
    assert _ids(page) == [inside_sent, inside_received]
    assert [item.direction for item in page.items] == ["sent", "received"]


def test_list_transactions_filters_never_return_rows_of_another_user(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    alice_rows = _spread(session_factory, alice, bob, 3)
    _store(session_factory, [_row(bob, carol, _at(1))])

    # Act
    page = _list(
        session_factory,
        alice,
        status="succeeded",
        method="p2p_transfer",
        created_from=_at(0),
        created_to=_at(10),
    )

    # Assert
    assert _ids(page) == alice_rows


def test_list_transactions_pages_through_a_filtered_range_without_duplicates_or_gaps(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = _spread(session_factory, alice, bob, 7)

    # Act
    pages, last_cursor = _walk_service(
        session_factory, alice, limit=2, created_from=_at(1), created_to=_at(6)
    )

    # Assert
    assert pages == [newest_first[1:3], newest_first[3:5], newest_first[5:6]]
    assert last_cursor is None


def test_list_transactions_rejects_a_naive_created_from_with_value_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    naive = datetime(2026, 11, 3, 12, 0, 0)

    # Act
    with pytest.raises(ValueError):
        _list(session_factory, alice, created_from=naive)

    # Assert
    # (pytest.raises pins the exception type)


def test_list_transactions_rejects_a_naive_created_to_with_value_error(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    naive = datetime(2026, 11, 3, 12, 0, 0)

    # Act
    with pytest.raises(ValueError):
        _list(session_factory, alice, created_to=naive)

    # Assert
    # (pytest.raises pins the exception type)


@pytest.mark.parametrize("cursor", list(INVALID_CURSORS.values()), ids=list(INVALID_CURSORS.keys()))
def test_list_transactions_raises_invalid_cursor_error_for_a_malformed_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock, cursor: str
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 2)

    # Act
    with pytest.raises(InvalidCursorError):
        _list(session_factory, alice, cursor=cursor)

    # Assert
    # (pytest.raises pins the exception type)


# --- GET /v1/me/transactions: shape, scoping, caching -----------------------------------------------------


def test_get_transactions_returns_200_with_the_exact_item_shape_for_a_sent_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    transaction_id = _send(session_factory, clock, alice, BOB, 2_500)

    # Act
    response = _get(client, _token(private_pem, clock, alice))

    # Assert
    body: dict[str, object] = response.json()
    items = _items(response)
    created_at = datetime.fromisoformat(str(items[0].pop("created_at")))
    assert response.status_code == 200
    assert {"items": items, "next_cursor": body["next_cursor"]} == {
        "items": [
            {
                "transaction_id": str(transaction_id),
                "method": "p2p_transfer",
                "direction": "sent",
                "status": "succeeded",
                "counterparty_email_masked": BOB_MASKED,
                "amount_minor": 2_500,
                "currency": "USD",
            }
        ],
        "next_cursor": None,
    }
    assert created_at == clock()


def test_get_transactions_shows_the_same_transfer_as_received_with_the_senders_masked_email(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    transaction_id = _send(session_factory, clock, alice, BOB, 2_500)

    # Act
    response = _get(client, _token(private_pem, clock, bob))

    # Assert
    items = _items(response)
    created_at = datetime.fromisoformat(str(items[0].pop("created_at")))
    assert response.status_code == 200
    assert items == [
        {
            "transaction_id": str(transaction_id),
            "method": "p2p_transfer",
            "direction": "received",
            "status": "succeeded",
            "counterparty_email_masked": ALICE_MASKED,
            "amount_minor": 2_500,
            "currency": "USD",
        }
    ]
    assert created_at == clock()


def test_get_transactions_for_a_user_without_transfers_returns_an_empty_list(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    _store(session_factory, [_row(bob, carol, _at(0))])

    # Act
    response = _get(client, _token(private_pem, clock, alice))

    # Assert
    assert (response.status_code, response.json()) == (200, {"items": [], "next_cursor": None})


def test_get_transactions_never_shows_other_users_transfers(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    a_to_b, c_to_a = _store(session_factory, [_row(alice, bob, _at(0)), _row(carol, alice, _at(1))])
    b_to_c = _store(session_factory, [_row(bob, carol, _at(2))])[0]

    # Act
    alice_response = _get(client, _token(private_pem, clock, alice))
    bob_response = _get(client, _token(private_pem, clock, bob))

    # Assert
    assert _item_ids(alice_response) == [str(c_to_a), str(a_to_b)]
    assert str(b_to_c) not in alice_response.text
    assert _item_ids(bob_response) == [str(b_to_c), str(a_to_b)]
    assert str(c_to_a) not in bob_response.text


def test_get_transactions_lists_newest_first_across_both_directions(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    oldest, middle, newest = _store(
        session_factory,
        [_row(alice, bob, _at(0)), _row(bob, alice, _at(1)), _row(alice, bob, _at(2))],
    )

    # Act
    response = _get(client, _token(private_pem, clock, alice))

    # Assert
    assert _item_ids(response) == [str(newest), str(middle), str(oldest)]
    assert [item["direction"] for item in _items(response)] == ["sent", "received", "sent"]


def test_get_transactions_forbids_caching_the_200(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 1)

    # Act
    response = _get(client, _token(private_pem, clock, alice))

    # Assert
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == NO_STORE


def test_get_transactions_body_contains_no_full_counterparty_email_and_no_user_or_account_id(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    carol = _sign_up(session_factory, clock, CAROL)
    _store(session_factory, [_row(alice, bob, _at(0)), _row(carol, alice, _at(1))])
    forbidden = [
        BOB,
        CAROL,
        str(alice.user_id),
        str(bob.user_id),
        str(carol.user_id),
        str(alice.account_id),
        str(bob.account_id),
        str(carol.account_id),
    ]

    # Act
    response = _get(client, _token(private_pem, clock, alice))

    # Assert
    assert response.status_code == 200
    assert [item["counterparty_email_masked"] for item in _items(response)] == [CAROL_MASKED, BOB_MASKED]
    assert [needle for needle in forbidden if needle in response.text] == []
    assert [set(item.keys()) for item in _items(response)] == [
        {
            "transaction_id",
            "method",
            "direction",
            "status",
            "counterparty_email_masked",
            "amount_minor",
            "currency",
            "created_at",
        }
    ] * 2


# --- GET /v1/me/transactions: paging ----------------------------------------------------------------------


def test_get_transactions_pages_with_limit_and_next_cursor_without_duplicates_or_gaps(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = [str(row_id) for row_id in _spread(session_factory, alice, bob, 5)]

    # Act
    pages, last_cursor = _walk_http(client, _token(private_pem, clock, alice), limit=2)

    # Assert
    assert pages == [expected[:2], expected[2:4], expected[4:]]
    assert last_cursor is None


def test_get_transactions_defaults_to_twenty_items(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = [str(row_id) for row_id in _spread(session_factory, alice, bob, DEFAULT_PAGE_SIZE + 1)]
    token = _token(private_pem, clock, alice)

    # Act
    first = _get(client, token)
    cursor = _next_cursor(first)
    assert cursor is not None
    second = _get(client, token, {"cursor": cursor})

    # Assert
    assert _item_ids(first) == expected[:DEFAULT_PAGE_SIZE]
    assert _item_ids(second) == expected[DEFAULT_PAGE_SIZE:]
    assert _next_cursor(second) is None


@pytest.mark.parametrize("limit", [1, 100])
def test_get_transactions_accepts_the_limit_bounds(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, limit: int
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = [str(row_id) for row_id in _spread(session_factory, alice, bob, 3)]

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"limit": limit})

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == expected[:limit]


@pytest.mark.parametrize("limit", ["0", "101", "-1", "abc"], ids=["zero", "above_max", "negative", "not_int"])
def test_get_transactions_rejects_an_out_of_range_limit_with_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, limit: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"limit": limit})

    # Assert
    assert response.status_code == 422


# --- GET /v1/me/transactions: filters ---------------------------------------------------------------------


def test_get_transactions_filters_by_a_half_open_created_range(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = [str(row_id) for row_id in _spread(session_factory, alice, bob, 5)]
    params: dict[str, str | int] = {
        "created_from": _at(1).isoformat(),
        "created_to": _at(4).isoformat(),
    }

    # Act
    response = _get(client, _token(private_pem, clock, alice), params)

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == newest_first[1:4]


def test_get_transactions_created_from_alone_is_inclusive(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = [str(row_id) for row_id in _spread(session_factory, alice, bob, 4)]

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"created_from": _at(1).isoformat()})

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == newest_first[:3]


def test_get_transactions_created_to_alone_is_exclusive(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = [str(row_id) for row_id in _spread(session_factory, alice, bob, 4)]

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"created_to": _at(3).isoformat()})

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == newest_first[1:]


def test_get_transactions_accepts_a_non_utc_offset_and_compares_instants(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = [str(row_id) for row_id in _spread(session_factory, alice, bob, 5)]
    params: dict[str, str | int] = {
        "created_from": _at(1).astimezone(JST).isoformat(),
        "created_to": _at(4).astimezone(JST).isoformat(),
    }

    # Act
    response = _get(client, _token(private_pem, clock, alice), params)

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == newest_first[1:4]


def test_get_transactions_accepts_the_status_and_method_filters(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = [str(row_id) for row_id in _spread(session_factory, alice, bob, 3)]

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"status": STATUS, "method": METHOD})

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == expected


def test_get_transactions_pages_through_a_filtered_range_without_duplicates_or_gaps(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    newest_first = [str(row_id) for row_id in _spread(session_factory, alice, bob, 7)]
    extra: dict[str, str | int] = {
        "created_from": _at(1).isoformat(),
        "created_to": _at(6).isoformat(),
        "status": STATUS,
        "method": METHOD,
    }

    # Act
    pages, last_cursor = _walk_http(client, _token(private_pem, clock, alice), limit=2, extra=extra)

    # Assert
    assert pages == [newest_first[1:3], newest_first[3:5], newest_first[5:6]]
    assert last_cursor is None


# --- GET /v1/me/transactions: 400 details -----------------------------------------------------------------

BAD_FILTERS = {
    "created_from_naive": ({"created_from": "2026-11-03T12:00:00"}, "invalid created_from"),
    "created_from_date_only": ({"created_from": "2026-11-03"}, "invalid created_from"),
    "created_from_garbage": ({"created_from": "yesterday"}, "invalid created_from"),
    "created_to_naive": ({"created_to": "2026-11-03T12:00:00"}, "invalid created_to"),
    "created_to_date_only": ({"created_to": "2026-11-03"}, "invalid created_to"),
    "created_to_garbage": ({"created_to": "tomorrow"}, "invalid created_to"),
    "range_inverted": (
        {"created_from": "2026-11-03T13:00:00+00:00", "created_to": "2026-11-03T12:00:00+00:00"},
        "invalid date range",
    ),
    "range_empty": (
        {"created_from": "2026-11-03T12:00:00+00:00", "created_to": "2026-11-03T12:00:00+00:00"},
        "invalid date range",
    ),
    "status_unknown": ({"status": "failed"}, "invalid status"),
    "status_case_sensitive": ({"status": "SUCCEEDED"}, "invalid status"),
    "method_unknown": ({"method": "card"}, "invalid method"),
    "method_case_sensitive": ({"method": "P2P_TRANSFER"}, "invalid method"),
    "cursor_garbage": ({"cursor": "not-a-cursor"}, "invalid cursor"),
    "cursor_naive_timestamp": (
        {"cursor": _cursor(f"2026-11-03T12:00:00|{VALID_CURSOR_ID}")},
        "invalid cursor",
    ),
}


@pytest.mark.parametrize(("params", "detail"), list(BAD_FILTERS.values()), ids=list(BAD_FILTERS.keys()))
def test_get_transactions_with_an_invalid_filter_returns_400_with_its_detail_and_no_store(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    private_pem: str,
    params: dict[str, str],
    detail: str,
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 2)
    query: dict[str, str | int] = dict(params)

    # Act
    response = _get(client, _token(private_pem, clock, alice), query)

    # Assert
    assert (response.status_code, response.json()) == (400, {"detail": detail})
    assert response.headers["Cache-Control"] == NO_STORE


@pytest.mark.parametrize("cursor", list(INVALID_CURSORS.values()), ids=list(INVALID_CURSORS.keys()))
def test_get_transactions_with_a_malformed_cursor_returns_400_invalid_cursor(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, cursor: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 2)

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"cursor": cursor})

    # Assert
    assert (response.status_code, response.json()) == (400, {"detail": "invalid cursor"})
    assert response.headers["Cache-Control"] == NO_STORE


# --- GET /v1/me/transactions: session ---------------------------------------------------------------------


def test_get_transactions_without_a_bearer_returns_401_missing_session_with_no_store(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 1)

    # Act
    response = _get(client, None)

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)
    assert response.headers["Cache-Control"] == NO_STORE


@pytest.mark.parametrize("kind", ["garbage", "expired"])
def test_get_transactions_with_an_unusable_session_token_returns_401_invalid_session(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, kind: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    tokens = {
        "garbage": "not-a-jwt",
        "expired": issue_session_token(private_pem, alice.user_id, clock() - timedelta(hours=1), ("pwd",)),
    }

    # Act
    response = _get(client, tokens[kind])

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_SESSION)
    assert response.headers["Cache-Control"] == NO_STORE
