"""Tests for `list_transfers`, `mask_email` and GET /v1/me/transfers, written from docs/specs/web.md
(Prerequisite P-1)."""

import base64
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import pyotp
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
from gateway.wallets.history import TransferHistoryItem, TransferPage, list_transfers, mask_email
from gateway.wallets.models import Transfer
from gateway.wallets.service import top_up_wallet, transfer
from httpx import Response
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_API_KEY_PEPPER, TEST_CARD_FINGERPRINT_KEY, TEST_NOW

HISTORY = "/v1/me/transfers"
SESSIONS = "/v1/sessions"
ENROL = "/v1/me/mfa/totp"
VERIFY = "/v1/me/mfa/totp/verify"
ALICE = "alice@example.com"
BOB = "bob@example.com"
CAROL = "carol@example.com"
DAVE = "dave@example.com"
ALICE_MASKED = "a***@example.com"
BOB_MASKED = "b***@example.com"
CAROL_MASKED = "c***@example.com"
PASSWORD = "history-test-password-7Q"
TOTP_KEY = "test-totp-encryption-key-0123456789abcdef"
STEP_SECONDS = 30
DEFAULT_PAGE_SIZE = 20
MISSING_SESSION = {"detail": "missing session"}
INVALID_SESSION = {"detail": "invalid session"}
MFA_REQUIRED = {"detail": "mfa required"}
INVALID_CURSOR = {"detail": "invalid cursor"}
VALID_CURSOR_ID = "00000000-0000-0000-0000-000000000001"
NO_STORE = "no-store"
NO_CACHE = "no-cache"
WALLETS = "/v1/me/wallets"


def _cursor(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode()


INVALID_CURSORS = {
    "garbage": "not-a-cursor",
    "bad_base64_padding": "a",
    "not_utf8": base64.urlsafe_b64encode(b"\xff\xfe|\xfd").decode(),
    "non_utc_offset": _cursor(f"2026-11-03T12:00:00+09:00|{VALID_CURSOR_ID}"),
    "naive_timestamp": _cursor(f"2026-11-03T12:00:00|{VALID_CURSOR_ID}"),
    "too_many_parts": _cursor(f"2026-11-03T12:00:00+00:00|{VALID_CURSOR_ID}|x"),
    "too_few_parts": _cursor("2026-11-03T12:00:00+00:00"),
    "bad_timestamp": _cursor(f"yesterday|{VALID_CURSOR_ID}"),
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
    currency: str = "USD",
) -> uuid.UUID:
    """A real transfer through the service; returns its id."""
    outcome = transfer(
        session_factory,
        clock,
        sender_user_id=sender.user_id,
        idempotency_key=f"k-{uuid.uuid4()}",
        recipient_email=recipient_email,
        amount_minor=amount_minor,
        currency=currency,
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
    """`count` transfers one minute apart; returns the ids newest first."""
    ids = _store(session_factory, [_row(sender, recipient, _at(minute)) for minute in range(count)])
    return list(reversed(ids))


def _list(
    session_factory: sessionmaker[Session],
    party: Party,
    limit: int = DEFAULT_PAGE_SIZE,
    cursor: str | None = None,
) -> TransferPage:
    with session_factory() as session:
        return list_transfers(session, party.user_id, limit, cursor)


def _list_with_defaults(session_factory: sessionmaker[Session], party: Party) -> TransferPage:
    """Calls `list_transfers` with neither `limit` nor `cursor`, to pin the signature's defaults."""
    with session_factory() as session:
        return list_transfers(session, party.user_id)


def _walk_service(
    session_factory: sessionmaker[Session], party: Party, limit: int
) -> tuple[list[list[uuid.UUID]], str | None]:
    """Follows next_cursor to the end; returns the ids per page and the last page's next_cursor."""
    pages: list[list[uuid.UUID]] = []
    cursor: str | None = None
    while True:
        page = _list(session_factory, party, limit, cursor)
        pages.append([item.transfer_id for item in page.items])
        if page.next_cursor is None:
            return pages, page.next_cursor
        cursor = page.next_cursor


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _token(private_pem: str, clock: FixedClock, party: Party, amr: tuple[str, ...] = ("pwd",)) -> str:
    return issue_session_token(private_pem, party.user_id, clock(), amr)


def _get(client: TestClient, token: str | None, params: dict[str, str | int] | None = None) -> Response:
    headers = {} if token is None else _bearer(token)
    response: Response = client.get(HISTORY, headers=headers, params=params)
    return response


def _items(response: Response) -> list[dict[str, object]]:
    items: list[dict[str, object]] = response.json()["items"]
    return items


def _item_ids(response: Response) -> list[str]:
    return [str(item["transfer_id"]) for item in _items(response)]


def _next_cursor(response: Response) -> str | None:
    body: dict[str, object] = response.json()
    cursor = body["next_cursor"]
    return None if cursor is None else str(cursor)


def _walk_http(client: TestClient, token: str, limit: int) -> tuple[list[list[str]], str | None]:
    pages: list[list[str]] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = _get(client, token, params)
        assert response.status_code == 200
        pages.append(_item_ids(response))
        cursor = _next_cursor(response)
        if cursor is None:
            return pages, cursor


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


# --- mask_email -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("alice@example.com", "a***@example.com"),
        ("a@example.com", "a***@example.com"),
        ("bob.smith+tag@mail.example.co.jp", "b***@mail.example.co.jp"),
        ("élodie@example.fr", "é***@example.fr"),
        ("7days@example.com", "7***@example.com"),
        ("first@odd@example.com", "f***@example.com"),
    ],
    ids=[
        "typical",
        "single_char_local",
        "plus_tag_and_subdomains",
        "non_ascii_first_char",
        "digit",
        "two_ats",
    ],
)
def test_mask_email_keeps_the_first_character_and_the_domain_only(email: str, masked: str) -> None:
    # Arrange
    # (parametrized)

    # Act
    result = mask_email(email)

    # Assert
    assert result == masked


def test_mask_email_does_not_leak_the_rest_of_the_local_part() -> None:
    # Arrange
    email = "alexander.hamilton@example.com"

    # Act
    result = mask_email(email)

    # Assert
    assert result == "a***@example.com"
    assert "lexander" not in result
    assert "hamilton" not in result


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("\u202ex@moc.knab", "****@moc.knab"),
        ("\u200bx@example.com", "****@example.com"),
        ("\u0007x@example.com", "****@example.com"),
        ("\u00a0x@example.com", "****@example.com"),
        ("\u0301x@example.com", "****@example.com"),
    ],
    ids=["bidi_rlo_cf", "zero_width_space_cf", "control_cc", "no_break_space_zs", "combining_acute_mn"],
)
def test_mask_email_replaces_a_leading_control_format_separator_or_mark_character_with_a_star(
    email: str, masked: str
) -> None:
    # Arrange
    # (parametrized)

    # Act
    result = mask_email(email)

    # Assert
    assert result == masked


@pytest.mark.parametrize(
    "email",
    ["alice", "", "@example.com"],
    ids=["no_at_sign", "empty_input", "empty_local_part"],
)
def test_mask_email_returns_a_fixed_placeholder_when_there_is_no_local_part_to_mask(email: str) -> None:
    # Arrange
    # (parametrized)

    # Act
    result = mask_email(email)

    # Assert
    assert result == "***"


def test_mask_email_placeholder_does_not_echo_an_input_without_an_at_sign() -> None:
    # Arrange
    email = "alexander.hamilton"

    # Act
    result = mask_email(email)

    # Assert
    assert result == "***"
    assert "alexander" not in result
    assert "hamilton" not in result


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("x@\u202emoc.knab", "x***@***"),
        ("a@exa\u200bmple.com", "a***@***"),
        ("a@example.com\u001b", "a***@***"),
        ("a@exa\u0000mple.com", "a***@***"),
        ("a@exa\u00a0mple.com", "a***@***"),
        ("a@e\u0301xample.com", "a***@***"),
        ("\u202ex@\u202emoc.knab", "****@***"),
    ],
    ids=[
        "domain_bidi_rlo_cf",
        "domain_zero_width_space_cf",
        "domain_escape_cc",
        "domain_nul_cc",
        "domain_no_break_space_zs",
        "domain_combining_acute_mn",
        "local_and_domain_both_unprintable",
    ],
)
def test_mask_email_replaces_a_domain_containing_a_control_format_separator_or_mark_character(
    email: str, masked: str
) -> None:
    # Arrange
    # (parametrized)

    # Act
    result = mask_email(email)

    # Assert
    assert result == masked


@pytest.mark.parametrize(
    ("email", "masked"),
    [
        ("a@bücher.example", "a***@bücher.example"),
        ("a@例え.jp", "a***@例え.jp"),
    ],
    ids=["idn_latin_precomposed", "idn_cjk"],
)
def test_mask_email_keeps_a_unicode_domain_without_control_format_separator_or_mark_characters(
    email: str, masked: str
) -> None:
    # Arrange
    # (parametrized)

    # Act
    result = mask_email(email)

    # Assert
    assert result == masked


# --- list_transfers: scoping and direction ----------------------------------------------------------------


def test_list_transfers_for_a_user_without_transfers_returns_an_empty_page(
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
    assert page == TransferPage(items=[], next_cursor=None)


def test_list_transfers_returns_only_transfers_where_the_user_is_sender_or_recipient(
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
    carol_page = _list(session_factory, carol)
    dave_page = _list(session_factory, dave)

    # Assert
    assert [item.transfer_id for item in alice_page.items] == [c_to_a, a_to_b]
    assert [item.transfer_id for item in bob_page.items] == [b_to_c, a_to_b]
    assert [item.transfer_id for item in carol_page.items] == [c_to_a, b_to_c]
    assert dave_page == TransferPage(items=[], next_cursor=None)


def test_list_transfers_shows_a_sent_item_to_the_sender_and_a_received_item_to_the_recipient(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    transfer_id = _send(session_factory, clock, alice, BOB, 2_500)

    # Act
    alice_page = _list(session_factory, alice)
    bob_page = _list(session_factory, bob)

    # Assert
    assert alice_page == TransferPage(
        items=[
            TransferHistoryItem(
                transfer_id=transfer_id,
                direction="sent",
                counterparty_email_masked=BOB_MASKED,
                amount_minor=2_500,
                currency="USD",
                created_at=clock(),
            )
        ],
        next_cursor=None,
    )
    assert bob_page == TransferPage(
        items=[
            TransferHistoryItem(
                transfer_id=transfer_id,
                direction="received",
                counterparty_email_masked=ALICE_MASKED,
                amount_minor=2_500,
                currency="USD",
                created_at=clock(),
            )
        ],
        next_cursor=None,
    )


def test_list_transfers_reports_each_amount_and_currency_of_a_users_mixed_history(
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
        TransferHistoryItem(
            transfer_id=received,
            direction="received",
            counterparty_email_masked=CAROL_MASKED,
            amount_minor=7,
            currency="EUR",
            created_at=_at(1),
        ),
        TransferHistoryItem(
            transfer_id=sent,
            direction="sent",
            counterparty_email_masked=BOB_MASKED,
            amount_minor=1_500,
            currency="JPY",
            created_at=_at(0),
        ),
    ]


# --- list_transfers: ordering -----------------------------------------------------------------------------


def test_list_transfers_returns_real_transfers_newest_first(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    first = _send(session_factory, clock, alice, BOB, 100)
    clock.advance(timedelta(minutes=1))
    second = _send(session_factory, clock, alice, BOB, 200)
    clock.advance(timedelta(minutes=1))
    third = _send(session_factory, clock, alice, BOB, 300)

    # Act
    page = _list(session_factory, alice)

    # Assert
    assert [(item.transfer_id, item.amount_minor) for item in page.items] == [
        (third, 300),
        (second, 200),
        (first, 100),
    ]


def test_list_transfers_breaks_a_created_at_tie_by_id_descending(
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
    assert [item.transfer_id for item in page.items] == [newer, high, middle, low]


# --- list_transfers: paging -------------------------------------------------------------------------------


def test_list_transfers_limit_truncates_the_page_and_chained_cursors_cover_every_row_once(
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
    assert [item.transfer_id for item in first_page.items] == expected[:2]
    assert pages == [expected[:2], expected[2:4], expected[4:]]
    assert last_cursor is None


def test_list_transfers_pages_through_a_created_at_tie_without_duplicates_or_gaps(
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


def test_list_transfers_has_no_next_cursor_when_the_rows_exactly_fill_the_limit(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 3)

    # Act
    page = _list(session_factory, alice, limit=3)

    # Assert
    assert [item.transfer_id for item in page.items] == expected
    assert page.next_cursor is None


def test_list_transfers_defaults_to_twenty_items_per_page(
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
    assert [item.transfer_id for item in first.items] == expected[:DEFAULT_PAGE_SIZE]
    assert [item.transfer_id for item in second.items] == expected[DEFAULT_PAGE_SIZE:]
    assert second.next_cursor is None


def test_list_transfers_cursor_never_returns_rows_of_another_user(
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
    assert [item.transfer_id for item in first.items + rest.items] == alice_rows


# --- list_transfers: invalid cursor -----------------------------------------------------------------------


@pytest.mark.parametrize("cursor", list(INVALID_CURSORS.values()), ids=list(INVALID_CURSORS.keys()))
def test_list_transfers_raises_invalid_cursor_error_for_a_malformed_cursor(
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


# --- GET /v1/me/transfers ---------------------------------------------------------------------------------


def test_get_history_returns_200_with_the_exact_item_shape_for_a_sent_transfer(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    transfer_id = _send(session_factory, clock, alice, BOB, 2_500)

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
                "transfer_id": str(transfer_id),
                "direction": "sent",
                "counterparty_email_masked": BOB_MASKED,
                "amount_minor": 2_500,
                "currency": "USD",
            }
        ],
        "next_cursor": None,
    }
    assert created_at == clock()


def test_get_history_shows_the_same_transfer_as_received_with_the_senders_masked_email(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _fund(session_factory, clock, alice, 10_000)
    transfer_id = _send(session_factory, clock, alice, BOB, 2_500)

    # Act
    response = _get(client, _token(private_pem, clock, bob))

    # Assert
    items = _items(response)
    created_at = datetime.fromisoformat(str(items[0].pop("created_at")))
    assert response.status_code == 200
    assert items == [
        {
            "transfer_id": str(transfer_id),
            "direction": "received",
            "counterparty_email_masked": ALICE_MASKED,
            "amount_minor": 2_500,
            "currency": "USD",
        }
    ]
    assert created_at == clock()


def test_get_history_never_shows_other_users_transfers(
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


def test_get_history_for_a_user_without_transfers_returns_an_empty_list(
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


def test_get_history_lists_newest_first_across_both_directions(
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


def test_get_history_breaks_a_created_at_tie_by_id_descending(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    ids = [uuid.UUID(int=n) for n in (1, 2, 3)]
    _store(session_factory, [_row(alice, bob, _at(4), transfer_id=row_id) for row_id in ids])

    # Act
    response = _get(client, _token(private_pem, clock, alice))

    # Assert
    assert _item_ids(response) == [str(ids[2]), str(ids[1]), str(ids[0])]


def test_get_history_pages_with_limit_and_next_cursor_without_duplicates_or_gaps(
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


def test_get_history_defaults_to_twenty_items(
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
def test_get_history_accepts_the_limit_bounds(
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
def test_get_history_rejects_an_out_of_range_limit_with_422(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, limit: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)

    # Act
    response = _get(client, _token(private_pem, clock, alice), {"limit": limit})

    # Assert
    assert response.status_code == 422


@pytest.mark.parametrize("cursor", list(INVALID_CURSORS.values()), ids=list(INVALID_CURSORS.keys()))
def test_get_history_with_an_invalid_cursor_returns_400_invalid_cursor_with_no_store(
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
    assert (response.status_code, response.json()) == (400, INVALID_CURSOR)
    assert response.headers["Cache-Control"] == NO_STORE


def test_get_history_forbids_caching_the_200(
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


# --- privacy ----------------------------------------------------------------------------------------------


def test_get_history_body_contains_no_full_counterparty_email_and_no_user_or_account_id(
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
        {"transfer_id", "direction", "counterparty_email_masked", "amount_minor", "currency", "created_at"}
    ] * 2


# --- session ----------------------------------------------------------------------------------------------


def test_get_history_without_a_bearer_returns_401_missing_session(
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


@pytest.mark.parametrize("kind", ["garbage", "expired"])
def test_get_history_with_an_unusable_session_token_returns_401_invalid_session(
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


def test_get_history_requires_an_otp_session_once_totp_is_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    _spread(session_factory, alice, bob, 1)
    _enable_totp(client, clock, ALICE)

    # Act
    response = _get(client, _token(private_pem, clock, alice, ("pwd",)))

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)


@pytest.mark.parametrize("path", [WALLETS, HISTORY], ids=["wallets", "transfers"])
def test_me_routes_401_missing_session_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, path: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    _sign_up(session_factory, clock, ALICE)

    # Act
    response = client.get(path)

    # Assert
    assert (response.status_code, response.json()) == (401, MISSING_SESSION)
    assert response.headers["Cache-Control"] == NO_STORE
    assert response.headers["Pragma"] == NO_CACHE


@pytest.mark.parametrize("path", [WALLETS, HISTORY], ids=["wallets", "transfers"])
@pytest.mark.parametrize("kind", ["garbage", "expired"])
def test_me_routes_401_invalid_session_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, path: str, kind: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    tokens = {
        "garbage": "not-a-jwt",
        "expired": issue_session_token(private_pem, alice.user_id, clock() - timedelta(hours=1), ("pwd",)),
    }

    # Act
    response = client.get(path, headers=_bearer(tokens[kind]))

    # Assert
    assert (response.status_code, response.json()) == (401, INVALID_SESSION)
    assert response.headers["Cache-Control"] == NO_STORE
    assert response.headers["Pragma"] == NO_CACHE


@pytest.mark.parametrize("path", [WALLETS, HISTORY], ids=["wallets", "transfers"])
def test_me_routes_401_mfa_required_forbids_caching(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str, path: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    _enable_totp(client, clock, ALICE)

    # Act
    response = client.get(path, headers=_bearer(_token(private_pem, clock, alice, ("pwd",))))

    # Assert
    assert (response.status_code, response.json()) == (401, MFA_REQUIRED)
    assert response.headers["Cache-Control"] == NO_STORE
    assert response.headers["Pragma"] == NO_CACHE


def test_get_history_accepts_an_otp_session_once_totp_is_enabled(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 1)
    _enable_totp(client, clock, ALICE)

    # Act
    response = _get(client, _token(private_pem, clock, alice, ("pwd", "otp")))

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == [str(expected[0])]


def test_get_history_accepts_a_password_only_session_for_a_user_without_totp(
    session_factory: sessionmaker[Session], clock: FixedClock, private_pem: str
) -> None:
    # Arrange
    client = _client(session_factory, clock, private_pem)
    alice = _sign_up(session_factory, clock, ALICE)
    bob = _sign_up(session_factory, clock, BOB)
    expected = _spread(session_factory, alice, bob, 1)

    # Act
    response = _get(client, _token(private_pem, clock, alice, ("pwd",)))

    # Assert
    assert response.status_code == 200
    assert _item_ids(response) == [str(expected[0])]
