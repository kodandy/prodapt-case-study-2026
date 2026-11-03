"""Tests for the card_ref on charge events, written from docs/specs/sq3.md section 2 (charge.py)."""

import hashlib
import hmac
import uuid

import pytest
from contracts.events import PaymentEventV1
from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.charge import ChargeDeps, charge_card
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.schemas import ChargeRequest
from gateway.payments.tokenization.cards import card_fingerprint
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_CARD_FINGERPRINT_KEY

APPROVE_PAN = "4242424242424242"
OTHER_PAN = "5555555555554444"
DECLINED_PAN = "4000000000000002"


def _request(pan: str, amount_minor: int = 1500) -> ChargeRequest:
    return ChargeRequest.model_validate(
        {
            "amount_minor": amount_minor,
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


def _events(session_factory: sessionmaker[Session]) -> list[PaymentEventV1]:
    with session_factory() as session:
        payloads = session.execute(text("SELECT payload FROM gateway.outbox ORDER BY created_at, event_id"))
        return [PaymentEventV1.model_validate(row.payload) for row in payloads]


def _expected_card_ref(pan: str, merchant_id: uuid.UUID) -> str:
    subkey = hmac.new(TEST_CARD_FINGERPRINT_KEY.encode(), b"card_ref-key:v1", hashlib.sha256).digest()
    fingerprint = card_fingerprint(pan, TEST_CARD_FINGERPRINT_KEY)
    message = f"{merchant_id}:{fingerprint}".encode()
    return hmac.new(subkey, message, hashlib.sha256).hexdigest()[:32]


def _add_merchant(session_factory: sessionmaker[Session], clock: FixedClock) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    with session_factory() as session:
        session.add(Merchant(id=merchant_id, name="Globex", created_at=clock()))
        session.commit()
    return merchant_id


@pytest.mark.parametrize("pan", [APPROVE_PAN, DECLINED_PAN], ids=["succeeded", "declined"])
def test_charge_event_carries_the_card_ref_derived_from_the_cards_fingerprint(
    session_factory: sessionmaker[Session], clock: FixedClock, pan: str
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)

    # Act
    charge_card(deps, merchant_id, "key-card-ref", _request(pan))

    # Assert
    events = _events(session_factory)
    assert [event.card_ref for event in events] == [_expected_card_ref(pan, merchant_id)]


def test_two_charges_with_the_same_test_pan_carry_the_same_card_ref(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)

    # Act
    charge_card(deps, merchant_id, "key-same-1", _request(APPROVE_PAN))
    charge_card(deps, merchant_id, "key-same-2", _request(APPROVE_PAN, amount_minor=1600))

    # Assert
    card_refs = [event.card_ref for event in _events(session_factory)]
    assert card_refs == [_expected_card_ref(APPROVE_PAN, merchant_id)] * 2


def test_charges_with_two_different_test_pans_carry_different_card_refs(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)

    # Act
    charge_card(deps, merchant_id, "key-pan-1", _request(APPROVE_PAN))
    charge_card(deps, merchant_id, "key-pan-2", _request(OTHER_PAN))

    # Assert
    card_refs = {event.card_ref for event in _events(session_factory)}
    assert card_refs == {
        _expected_card_ref(APPROVE_PAN, merchant_id),
        _expected_card_ref(OTHER_PAN, merchant_id),
    }
    assert len(card_refs) == 2


def test_the_same_test_pan_at_two_merchants_carries_different_card_refs(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, first_merchant = _deps_and_merchant(session_factory, clock)
    second_merchant = _add_merchant(session_factory, clock)

    # Act
    charge_card(deps, first_merchant, "key-merchant-1", _request(APPROVE_PAN))
    charge_card(deps, second_merchant, "key-merchant-2", _request(APPROVE_PAN))

    # Assert
    by_merchant = {event.merchant_id: event.card_ref for event in _events(session_factory)}
    assert by_merchant == {
        first_merchant: _expected_card_ref(APPROVE_PAN, first_merchant),
        second_merchant: _expected_card_ref(APPROVE_PAN, second_merchant),
    }
    assert by_merchant[first_merchant] != by_merchant[second_merchant]


def test_charges_at_other_merchants_do_not_change_the_card_ref_a_merchant_gets_for_the_same_pan(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)
    other_merchant = _add_merchant(session_factory, clock)

    # Act
    charge_card(deps, merchant_id, "key-own-1", _request(DECLINED_PAN))
    charge_card(deps, other_merchant, "key-other-1", _request(DECLINED_PAN))
    charge_card(deps, merchant_id, "key-own-2", _request(DECLINED_PAN, amount_minor=1700))

    # Assert
    own_refs = [event.card_ref for event in _events(session_factory) if event.merchant_id == merchant_id]
    assert own_refs == [_expected_card_ref(DECLINED_PAN, merchant_id)] * 2


def test_the_outbox_payload_holds_the_card_ref_but_neither_the_pan_nor_the_fingerprint(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)
    fingerprint = card_fingerprint(APPROVE_PAN, TEST_CARD_FINGERPRINT_KEY)

    # Act
    charge_card(deps, merchant_id, "key-privacy", _request(APPROVE_PAN))

    # Assert
    with session_factory() as session:
        payload_text = str(session.execute(text("SELECT payload::text FROM gateway.outbox")).scalar_one())
    assert _expected_card_ref(APPROVE_PAN, merchant_id) in payload_text
    assert APPROVE_PAN not in payload_text
    assert fingerprint not in payload_text
    assert fingerprint[:32] not in payload_text


def test_the_rest_of_the_charge_event_is_unchanged_by_the_card_ref(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    deps, merchant_id = _deps_and_merchant(session_factory, clock)

    # Act
    outcome = charge_card(deps, merchant_id, "key-unchanged", _request(APPROVE_PAN))

    # Assert
    (event,) = _events(session_factory)
    assert event.model_dump(exclude={"event_id", "entity_version", "card_ref"}) == {
        "event_type": "payment.succeeded",
        "occurred_at": clock(),
        "origin": "live",
        "payment_id": uuid.UUID(str(outcome.body["payment_id"])),
        "merchant_id": merchant_id,
        "amount_minor": 1500,
        "currency": "USD",
        "method": "card",
        "state": "succeeded",
        "provider": "acq_a",
        "decline_code": None,
        "card_bin": "424242",
        "card_last4": "4242",
    }
