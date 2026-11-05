"""Live Stripe test-mode charges. Runs only when STRIPE_SECRET_KEY (an sk_test_ key) is set."""

import os
import uuid

import pytest
from gateway.payments.providers.port import CardRef
from gateway.payments.providers.stripe.provider import StripeProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("STRIPE_SECRET_KEY"),
    reason="STRIPE_SECRET_KEY is not set: no live Stripe test-mode run",
)

CARD_TOKEN = "tok_gateway_vault_live_test"
VISA = CardRef(token=CARD_TOKEN, bin="424242", last4="4242")
CHARGE_DECLINED = CardRef(token=CARD_TOKEN, bin="400000", last4="0002")


@pytest.fixture
def provider() -> StripeProvider:
    return StripeProvider(os.environ["STRIPE_SECRET_KEY"])


def _unique_key() -> str:
    return f"gw-live-test-{uuid.uuid4().hex}"


def test_live_charge_with_pm_card_visa_succeeds(provider: StripeProvider) -> None:
    # Arrange
    provider_key = _unique_key()

    # Act
    result = provider.charge(provider_key, 1999, "eur", VISA)

    # Assert
    assert result.status == "succeeded"
    assert result.provider_ref.startswith("pi_")
    assert result.decline_code is None


def test_live_charge_with_the_charge_declined_card_is_declined(provider: StripeProvider) -> None:
    # Arrange
    provider_key = _unique_key()

    # Act
    result = provider.charge(provider_key, 1999, "eur", CHARGE_DECLINED)

    # Assert
    assert result.status == "declined"
    assert result.provider_ref.startswith("pi_")
    assert result.decline_code == "card_declined"


def test_live_charge_repeated_with_the_same_provider_key_returns_the_same_payment_intent(
    provider: StripeProvider,
) -> None:
    # Arrange
    provider_key = _unique_key()
    first = provider.charge(provider_key, 1999, "eur", VISA)

    # Act
    second = provider.charge(provider_key, 1999, "eur", VISA)

    # Assert
    assert second == first
