"""Tests for the SQ1 test card, written from docs/specs/sq1.md section 1 (gateway test card)."""

import uuid

from contracts.events import PAYMENTS_TOPIC, PaymentEventV1
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import DECLINES_BY_CARD, SimulatorProvider
from gateway.payments.schemas import CardSummary, ChargeRequest, ChargeResponse
from gateway.payments.tokenization.cards import TEST_PANS, luhn_valid
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

SQ1_PAN = "4000000000000119"
EXISTING_TEST_PANS = frozenset(
    {"4242424242424242", "5555555555554444", "4000000000009995", "4000000000000002"}
)


def _request(pan: str) -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": 1500,
            "currency": "USD",
            "method": "card",
            "card": {"number": pan, "exp_month": 12, "exp_year": 2030, "cvc": "123"},
        }
    )


def _deps_and_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> tuple[ChargeDeps, uuid.UUID]:
    merchant_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Merchant(id=merchant_id, name="Acme", created_at=clock()))
        session.commit()
    deps = ChargeDeps(
        session_factory=session_factory,
        redis=None,
        provider=SimulatorProvider(session_factory, clock),
        clock=clock,
        card_fingerprint_key=TEST_CARD_FINGERPRINT_KEY,
    )
    return deps, merchant_id


def _outbox_payloads(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [
            dict(row) for row in session.execute(text("SELECT topic, payload FROM gateway.outbox")).mappings()
        ]


def test_test_pans_allowlist_is_the_four_existing_cards_plus_the_sq1_card() -> None:
    # Arrange
    expected = EXISTING_TEST_PANS | {SQ1_PAN}

    # Act
    allowlist = TEST_PANS

    # Assert
    assert allowlist == expected


def test_luhn_valid_accepts_the_sq1_card() -> None:
    # Arrange: the SQ1 card is a published-style test number on BIN 400000

    # Act
    result = luhn_valid(SQ1_PAN)

    # Assert
    assert result is True


def test_declines_by_card_maps_bin_400000_last4_0119_to_issuer_unavailable() -> None:
    # Arrange
    key = ("400000", "0119")

    # Act
    decline_code = DECLINES_BY_CARD[key]

    # Assert
    assert decline_code == "issuer_unavailable"


def test_declines_by_card_keeps_the_existing_decline_cards() -> None:
    # Arrange / Act
    table = dict(DECLINES_BY_CARD)

    # Assert
    assert table == {
        ("400000", "9995"): "insufficient_funds",
        ("400000", "0002"): "card_declined",
        ("400000", "0119"): "issuer_unavailable",
    }


def test_charge_card_with_the_sq1_card_returns_201_declined_issuer_unavailable(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)

    # Act
    outcome = charge_card(deps, merchant_id, "key-sq1", _request(SQ1_PAN))

    # Assert
    assert outcome.status_code == 201
    assert outcome.body == ChargeResponse(
        payment_id=uuid.UUID(str(outcome.body["payment_id"])),
        state="declined",
        amount_minor=1500,
        currency="USD",
        method="card",
        card=CardSummary(bin="400000", last4="0119"),
        decline_code="issuer_unavailable",
    ).model_dump(mode="json")


def test_charge_card_with_the_sq1_card_emits_a_declined_event_on_bin_400000(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)

    # Act
    outcome = charge_card(deps, merchant_id, "key-sq1-event", _request(SQ1_PAN))

    # Assert
    rows = _outbox_payloads(session_factory)
    assert [row["topic"] for row in rows] == [PAYMENTS_TOPIC]
    event = PaymentEventV1.model_validate(rows[0]["payload"])
    assert event.event_type == "payment.declined"
    assert event.state == "declined"
    assert event.payment_id == uuid.UUID(str(outcome.body["payment_id"]))
    assert event.merchant_id == merchant_id
    assert (event.card_bin, event.card_last4, event.decline_code) == ("400000", "0119", "issuer_unavailable")
