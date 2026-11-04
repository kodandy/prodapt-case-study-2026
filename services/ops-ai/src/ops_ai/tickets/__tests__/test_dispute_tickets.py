"""Tests for dispute tickets (open_dispute_ticket, seed_settlement wiring), from the B12 tickets spec (4b)."""

import uuid
from datetime import UTC, datetime, timedelta

from ops_ai.seed import merchant_uuid
from ops_ai.settlement.seed import seed_settlement
from ops_ai.tickets.service import TicketOpened
from ops_ai.tickets.sources import open_dispute_ticket
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW
from scripts.generate_dataset import (
    CanonicalTransaction,
    PaymentMethod,
    PlantedScenarios,
    RecordKind,
    ScenarioLog,
    Status,
)

WINDOW_END = datetime(2026, 10, 31, 18, 0, tzinfo=UTC)
ANCHOR = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
PAYMENT = uuid.UUID("cccccccc-0000-4000-8000-000000000003")
TICKETS_QUERY = "SELECT * FROM ops_ai.tickets ORDER BY source_key"


def _tx(
    n: int,
    *,
    record_kind: RecordKind = "merchant_payment",
    status: Status = "success",
    payment_method: PaymentMethod = "card",
    merchant_id: str | None = "mch_0003",
    chargeback_flag: bool = False,
    amount_minor: int = 12_345,
    currency: str = "USD",
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=uuid.UUID(int=n),
        user_id=uuid.UUID(int=1000 + n),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        payment_method=payment_method,
        status=status,
        failure_reason=None,
        fraud_score=0.1,
        timestamp=WINDOW_END - timedelta(hours=1),
        chargeback_flag=chargeback_flag,
        settlement_status="settled",
        resolution_notes=None,
        kyc_status="verified",
        country_sender="US",
        country_receiver="US",
        record_kind=record_kind,
        scenario="baseline",
        card_bin="424242",
        acquirer="acq_a",
        rule_id=None,
        sender_balance_minor=None,
    )


def _log(*transactions: CanonicalTransaction) -> ScenarioLog:
    planted = PlantedScenarios(
        sq1_merchant_id="mch_0001",
        sq1_window_start=WINDOW_END - timedelta(hours=6),
        sq1_window_end=WINDOW_END,
        sq1_card_bin="400000",
        sq2_transaction_id=uuid.UUID(int=2),
        sq3_merchant_id="mch_0002",
        sq3_transaction_ids=(uuid.UUID(int=3),),
        sq4_merchant_id="mch_0003",
    )
    return ScenarioLog(transactions=transactions, incidents=(), planted=planted)


def _tickets(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(text(TICKETS_QUERY)).mappings()]


def _open_dispute(
    session_factory: sessionmaker[Session], payment_id: uuid.UUID = PAYMENT, amount_minor: int = 4_200
) -> TicketOpened:
    with session_factory.begin() as session:
        return open_dispute_ticket(
            session,
            lambda: TEST_NOW,
            payment_id=payment_id,
            merchant_id=MERCHANT,
            amount_minor=amount_minor,
            currency="EUR",
        )


def test_open_dispute_ticket_creates_a_high_priority_compliance_ticket_with_the_chargeback_reason(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange / Act
    opened = _open_dispute(session_factory)

    # Assert
    (row,) = _tickets(session_factory)
    assert opened.created is True
    assert row == {
        "ticket_id": opened.ticket_id,
        "source_key": f"dispute:{PAYMENT}",
        "source": "dispute",
        "queue": "compliance",
        "priority": "high",
        "status": "open",
        "assignee": None,
        "merchant_id": MERCHANT,
        "title": "Chargeback received",
        "reason": (
            f"Chargeback on payment {PAYMENT}: 4200 EUR (minor units) per the acquirer settlement report"
        ),
        "alert_id": None,
        "created_at": TEST_NOW,
        "updated_at": None,
    }


def test_open_dispute_ticket_twice_for_one_payment_returns_the_first_ticket_and_keeps_one_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _open_dispute(session_factory, amount_minor=4_200)

    # Act
    second = _open_dispute(session_factory, amount_minor=9_999)

    # Assert
    (row,) = _tickets(session_factory)
    assert second == TicketOpened(ticket_id=first.ticket_id, created=False)
    assert "4200 EUR" in str(row["reason"])


def test_open_dispute_ticket_for_two_payments_creates_two_tickets(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    other_payment = uuid.UUID("dddddddd-0000-4000-8000-000000000004")

    # Act
    _open_dispute(session_factory, PAYMENT)
    _open_dispute(session_factory, other_payment)

    # Assert
    assert [row["source_key"] for row in _tickets(session_factory)] == sorted(
        [f"dispute:{PAYMENT}", f"dispute:{other_payment}"]
    )


def test_seed_settlement_opens_one_dispute_ticket_per_chargeback_entry_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    plain = _tx(10)
    refunded = _tx(11, status="reversed", amount_minor=2_000, currency="EUR")
    charged_back = _tx(12, chargeback_flag=True, amount_minor=500_000, currency="JPY")
    log = _log(plain, refunded, charged_back)

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    (row,) = _tickets(session_factory)
    assert inserted == 3
    assert row["source_key"] == f"dispute:{charged_back.transaction_id}"
    assert row["source"] == "dispute"
    assert (row["queue"], row["priority"], row["status"]) == ("compliance", "high", "open")
    assert row["merchant_id"] == merchant_uuid("mch_0003")
    assert row["title"] == "Chargeback received"
    assert row["reason"] == (
        f"Chargeback on payment {charged_back.transaction_id}: 500000 JPY (minor units) "
        "per the acquirer settlement report"
    )


def test_seed_settlement_opens_a_ticket_for_every_chargeback_entry(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    first = _tx(10, chargeback_flag=True, merchant_id="mch_0003")
    second = _tx(11, chargeback_flag=True, merchant_id="mch_0017", amount_minor=700)
    log = _log(first, second, _tx(12))

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    rows = _tickets(session_factory)
    assert inserted == 3
    assert sorted((str(row["source_key"]), str(row["merchant_id"])) for row in rows) == sorted(
        [
            (f"dispute:{first.transaction_id}", str(merchant_uuid("mch_0003"))),
            (f"dispute:{second.transaction_id}", str(merchant_uuid("mch_0017"))),
        ]
    )


def test_seed_settlement_opens_a_ticket_for_a_reversed_payment_that_was_also_charged_back(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    both = _tx(10, status="reversed", chargeback_flag=True)

    # Act
    seed_settlement(session_factory, _log(both), ANCHOR)

    # Assert
    assert [row["source_key"] for row in _tickets(session_factory)] == [f"dispute:{both.transaction_id}"]


def test_seed_settlement_without_a_chargeback_opens_no_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log(_tx(10), _tx(11, status="reversed"))

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 2
    assert _tickets(session_factory) == []


def test_seed_settlement_opens_no_ticket_for_a_chargeback_that_is_not_a_captured_card_payment(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log(
        _tx(10, status="failed", chargeback_flag=True),
        _tx(11, payment_method="wallet", chargeback_flag=True),
        _tx(12, record_kind="p2p_transfer", chargeback_flag=True),
    )

    # Act
    inserted = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert inserted == 0
    assert _tickets(session_factory) == []


def test_a_second_seed_run_returns_zero_and_opens_no_second_ticket(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log(_tx(10, chargeback_flag=True), _tx(11))
    seed_settlement(session_factory, log, ANCHOR)
    (before,) = _tickets(session_factory)

    # Act
    rerun = seed_settlement(session_factory, log, ANCHOR)

    # Assert
    assert rerun == 0
    assert _tickets(session_factory) == [before]


def test_a_later_seed_run_opens_tickets_only_for_the_newly_inserted_chargebacks(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    old = _tx(10, chargeback_flag=True)
    new = _tx(13, chargeback_flag=True)
    seed_settlement(session_factory, _log(old), ANCHOR)

    # Act
    inserted = seed_settlement(session_factory, _log(old, new), ANCHOR)

    # Assert
    assert inserted == 1
    assert [row["source_key"] for row in _tickets(session_factory)] == sorted(
        [f"dispute:{old.transaction_id}", f"dispute:{new.transaction_id}"]
    )


def test_a_chargeback_ticket_that_is_already_assigned_is_left_alone_by_a_seed_rerun(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _log(_tx(10, chargeback_flag=True))
    seed_settlement(session_factory, log, ANCHOR)
    with session_factory.begin() as session:
        session.execute(text("UPDATE ops_ai.tickets SET status = 'assigned', assignee = 'alice'"))

    # Act
    seed_settlement(session_factory, log, ANCHOR)

    # Assert
    (row,) = _tickets(session_factory)
    assert (row["status"], row["assignee"]) == ("assigned", "alice")
