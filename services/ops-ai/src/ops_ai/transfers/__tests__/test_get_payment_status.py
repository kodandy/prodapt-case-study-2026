"""Tests for the get_payment_status tool, written from docs/specs/sq2.md section 5."""

import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.scope import UserScope
from ops_ai.transfers import TransferStatus, get_payment_status
from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
BASE_TIME = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
RULE_ID = "xb_limit_kyc_tier1_v3"

INSERT = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, :amount_minor, :currency, :method, :status, :country_sender, "
    ":country_receiver, :reason_code, :rule_id, :kyc_tier, :limit_minor, :limit_currency, "
    ":next_tier_limit_minor, :sender_balance_minor, :decided_at, :ingest_source)"
)


def _insert(session_factory: sessionmaker[Session], **overrides: object) -> uuid.UUID:
    params: dict[str, object] = {
        "transfer_id": uuid.uuid4(),
        "sender_user_id": SENDER,
        "amount_minor": 180_000,
        "currency": "USD",
        "method": "bank_transfer",
        "status": "failed",
        "country_sender": "US",
        "country_receiver": "IN",
        "reason_code": "cross_border_limit_kyc_tier",
        "rule_id": RULE_ID,
        "kyc_tier": 1,
        "limit_minor": 100_000,
        "limit_currency": "USD",
        "next_tier_limit_minor": 1_000_000,
        "sender_balance_minor": 525_000,
        "decided_at": BASE_TIME,
        "ingest_source": "seed",
    }
    params.update(overrides)
    with session_factory.begin() as session:
        session.execute(INSERT, params)
    transfer_id = params["transfer_id"]
    assert isinstance(transfer_id, uuid.UUID)
    return transfer_id


def _status(
    session_factory: sessionmaker[Session], user_id: uuid.UUID, transfer_id: uuid.UUID | None
) -> TransferStatus | None:
    with session_factory() as session:
        return get_payment_status(session, UserScope(user_id=user_id), transfer_id)


def test_transfer_status_has_the_specified_fields_in_order_and_no_rule_id() -> None:
    # Arrange / Act
    names = [field.name for field in dataclasses.fields(TransferStatus)]

    # Assert
    assert names == [
        "transfer_id",
        "amount_minor",
        "currency",
        "method",
        "status",
        "country_sender",
        "country_receiver",
        "reason_code",
        "kyc_tier",
        "limit_minor",
        "limit_currency",
        "next_tier_limit_minor",
        "sender_balance_minor",
        "decided_at",
    ]


def test_get_payment_status_returns_the_own_transfer_by_id_without_the_rule_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _status(session_factory, SENDER, transfer_id)

    # Assert
    assert result == TransferStatus(
        transfer_id,
        180_000,
        "USD",
        "bank_transfer",
        "failed",
        "US",
        "IN",
        "cross_border_limit_kyc_tier",
        1,
        100_000,
        "USD",
        1_000_000,
        525_000,
        BASE_TIME,
    )
    assert RULE_ID not in repr(result)


def test_get_payment_status_by_id_also_returns_a_transfer_that_did_not_fail(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(
        session_factory,
        status="success",
        reason_code=None,
        rule_id=None,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
    )

    # Act
    result = _status(session_factory, SENDER, transfer_id)

    # Assert
    assert result is not None
    assert (result.transfer_id, result.status, result.reason_code) == (transfer_id, "success", None)
    assert (result.kyc_tier, result.limit_minor, result.limit_currency) == (None, None, None)


def test_get_payment_status_returns_none_for_another_senders_transfer_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    others_transfer = _insert(session_factory, sender_user_id=OTHER_SENDER)
    _insert(session_factory)

    # Act
    result = _status(session_factory, SENDER, others_transfer)

    # Assert
    assert result is None


def test_get_payment_status_returns_none_for_a_transfer_id_that_does_not_exist(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory)

    # Act
    result = _status(session_factory, SENDER, uuid.UUID("deadbeef-0000-4000-8000-000000000000"))

    # Assert
    assert result is None


def test_get_payment_status_without_an_id_returns_the_newest_failed_transfer_of_the_sender(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory, decided_at=BASE_TIME - timedelta(hours=3))
    newest_failed = _insert(session_factory, decided_at=BASE_TIME)
    _insert(session_factory, decided_at=BASE_TIME - timedelta(hours=1))

    # Act
    result = _status(session_factory, SENDER, None)

    # Assert
    assert result is not None
    assert result.transfer_id == newest_failed


def test_get_payment_status_without_an_id_skips_newer_transfers_that_did_not_fail(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    older_failed = _insert(session_factory, decided_at=BASE_TIME - timedelta(hours=2))
    _insert(session_factory, status="success", reason_code=None, decided_at=BASE_TIME)
    _insert(session_factory, status="pending", reason_code=None, decided_at=BASE_TIME - timedelta(hours=1))

    # Act
    result = _status(session_factory, SENDER, None)

    # Assert
    assert result is not None
    assert result.transfer_id == older_failed


def test_get_payment_status_without_an_id_never_returns_a_newer_failed_transfer_of_another_sender(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    own = _insert(session_factory, decided_at=BASE_TIME - timedelta(hours=5))
    _insert(session_factory, sender_user_id=OTHER_SENDER, decided_at=BASE_TIME)

    # Act
    result = _status(session_factory, SENDER, None)

    # Assert
    assert result is not None
    assert result.transfer_id == own


def test_get_payment_status_without_an_id_returns_none_when_the_sender_has_no_failed_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory, status="success", reason_code=None)
    _insert(session_factory, sender_user_id=OTHER_SENDER)

    # Act
    result = _status(session_factory, SENDER, None)

    # Assert
    assert result is None


def test_get_payment_status_returns_none_for_a_sender_with_no_transfers_at_all(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory, sender_user_id=OTHER_SENDER)

    # Act
    result = _status(session_factory, SENDER, None)

    # Assert
    assert result is None


def test_get_payment_status_filters_by_the_sender_in_the_query_itself(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, sender_user_id=OTHER_SENDER)
    statements: list[tuple[str, object]] = []

    def capture(_conn: object, _cursor: object, statement: str, parameters: object, *_rest: object) -> None:
        statements.append((statement, parameters))

    with session_factory() as session:
        bind = session.get_bind()
        event.listen(bind, "before_cursor_execute", capture)
        try:
            # Act
            result = get_payment_status(session, UserScope(user_id=SENDER), transfer_id)
        finally:
            event.remove(bind, "before_cursor_execute", capture)

    # Assert
    queries = [(sql, params) for sql, params in statements if "transfer_facts" in sql]
    assert result is None
    assert len(queries) == 1
    sql, params = queries[0]
    assert "sender_user_id =" in sql
    assert str(SENDER) in repr(params)
