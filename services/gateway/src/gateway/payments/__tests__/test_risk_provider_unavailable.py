"""Risk scoring ignores declines caused by an unavailable provider (decline_code "provider_unavailable"):
nothing was charged and the cardholder did nothing wrong, so they must not feed the `recent_declines` rule."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from gateway.accounts.models import Merchant
from gateway.clock import FixedClock
from gateway.payments.models import Payment
from gateway.payments.risk import DECLINE_LIMIT, RiskAssessment, assess_charge_risk
from gateway.payments.tokenization.models import CardToken
from gateway.resilience.circuit_breaker import PROVIDER_UNAVAILABLE
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

FINGERPRINT = "c" * 64
SMALL_AMOUNT = 1500
ORDINARY_DECLINE = "card_declined"
NO_RISK = RiskAssessment(score=Decimal("0.00"), reasons=(), flagged=False, blocked=False)


def _seed_merchant(session: Session, now: datetime) -> uuid.UUID:
    merchant_id = uuid.uuid4()
    session.add(Merchant(id=merchant_id, name="Acme", created_at=now))
    session.flush()
    return merchant_id


def _seed_declined(
    session: Session, merchant_id: uuid.UUID, created_at: datetime, decline_code: str | None
) -> None:
    token = f"tok_{uuid.uuid4().hex}"
    payment_id = uuid.uuid4()
    session.add(
        CardToken(
            token=token,
            fingerprint=FINGERPRINT,
            bin="424242",
            last4="4242",
            exp_month=12,
            exp_year=2030,
            created_at=created_at,
        )
    )
    session.flush()  # payments.card_token has an FK but no relationship(), so order the inserts by hand
    session.add(
        Payment(
            id=payment_id,
            merchant_id=merchant_id,
            amount_minor=SMALL_AMOUNT,
            currency="USD",
            method="card",
            state="declined",
            decline_code=decline_code,
            card_token=token,
            entity_version=1,
            created_at=created_at,
            updated_at=created_at,
        )
    )
    session.flush()
    session.execute(
        text("UPDATE gateway.payments SET card_fingerprint = :fp WHERE id = :id"),
        {"fp": FINGERPRINT, "id": payment_id},
    )


def _assess_with_declines(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    decline_codes: list[str | None],
    age: timedelta = timedelta(minutes=30),
) -> RiskAssessment:
    now = clock()
    with session_factory() as session:
        merchant_id = _seed_merchant(session, now)
        for decline_code in decline_codes:
            _seed_declined(session, merchant_id, now - age, decline_code)
        return assess_charge_risk(session, now, FINGERPRINT, SMALL_AMOUNT, "USD")


def test_decline_limit_is_two() -> None:
    # Arrange / Act
    limit = DECLINE_LIMIT

    # Assert
    assert limit == 2


def test_provider_unavailable_declines_alone_do_not_trigger_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    codes: list[str | None] = [PROVIDER_UNAVAILABLE, PROVIDER_UNAVAILABLE]

    # Act
    result = _assess_with_declines(session_factory, clock, codes)

    # Assert
    assert result == NO_RISK


def test_ordinary_declines_at_the_limit_still_trigger_recent_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    codes: list[str | None] = [ORDINARY_DECLINE, ORDINARY_DECLINE]

    # Act
    result = _assess_with_declines(session_factory, clock, codes)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.30"), reasons=("recent_declines",), flagged=False, blocked=False
    )


def test_provider_unavailable_declines_are_not_added_to_one_ordinary_decline(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    codes: list[str | None] = [ORDINARY_DECLINE, PROVIDER_UNAVAILABLE, PROVIDER_UNAVAILABLE]

    # Act
    result = _assess_with_declines(session_factory, clock, codes)

    # Assert
    assert result == NO_RISK


def test_declines_without_a_decline_code_still_count_as_declines(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    codes: list[str | None] = [None, None]

    # Act
    result = _assess_with_declines(session_factory, clock, codes)

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.30"), reasons=("recent_declines",), flagged=False, blocked=False
    )


def test_provider_unavailable_payments_still_count_towards_card_velocity(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    codes: list[str | None] = [PROVIDER_UNAVAILABLE] * 3

    # Act
    result = _assess_with_declines(session_factory, clock, codes, age=timedelta(minutes=1))

    # Assert
    assert result == RiskAssessment(
        score=Decimal("0.40"), reasons=("card_velocity",), flagged=False, blocked=False
    )
