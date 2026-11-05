"""Provider selection: build_provider and the PAYMENT_PROVIDER / STRIPE_SECRET_KEY settings."""

import pytest
from gateway.clock import FixedClock
from gateway.config import GatewaySettings
from gateway.payments.providers.factory import ProviderConfigError, build_provider
from gateway.payments.providers.simulator.provider import SimulatorProvider
from gateway.payments.providers.stripe.provider import LiveKeyRefusedError, StripeProvider
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

TEST_KEY = "sk_test_FAKEunitTESTkeyNOTreal0000"
LIVE_KEY = "sk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET"
SECRET_32 = "k" * 32
DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
REDIS_URL = "redis://localhost:6379/0"


def _unbound_sessions() -> sessionmaker[Session]:
    return sessionmaker()


def _clock() -> FixedClock:
    return FixedClock(TEST_NOW)


class TestBuildProvider:
    def test_simulator_returns_a_simulator_provider(self, session_factory: sessionmaker[Session]) -> None:
        # Arrange
        clock = _clock()

        # Act
        provider = build_provider("simulator", None, session_factory, clock)

        # Assert
        assert isinstance(provider, SimulatorProvider)
        assert provider.name == "acq_a"

    def test_simulator_ignores_a_stripe_key(self, session_factory: sessionmaker[Session]) -> None:
        # Arrange
        clock = _clock()

        # Act
        provider = build_provider("simulator", TEST_KEY, session_factory, clock)

        # Assert
        assert isinstance(provider, SimulatorProvider)

    def test_stripe_with_a_test_key_returns_a_stripe_provider(self) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act
        provider = build_provider("stripe", TEST_KEY, sessions, _clock())

        # Assert
        assert isinstance(provider, StripeProvider)
        assert provider.name == "stripe"

    @pytest.mark.parametrize("key", [None, ""])
    def test_stripe_without_a_key_is_a_config_error(self, key: str | None) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act / Assert
        with pytest.raises(ProviderConfigError):
            build_provider("stripe", key, sessions, _clock())

    @pytest.mark.parametrize(
        "key",
        [
            LIVE_KEY,
            "rk_live_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
            "pk_test_51HqLyjWDarjtT1zdp7dcSECRETSECRET",
        ],
    )
    def test_stripe_with_a_non_test_key_is_refused_without_echoing_the_key(self, key: str) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act
        with pytest.raises(LiveKeyRefusedError) as raised:
            build_provider("stripe", key, sessions, _clock())

        # Assert
        assert key not in str(raised.value)
        assert key not in repr(raised.value)
        assert key[8:] not in str(raised.value)

    @pytest.mark.parametrize("name", ["paypal", "", "acq_a", "stripe "])
    def test_an_unknown_provider_name_is_a_config_error(self, name: str) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act / Assert
        with pytest.raises(ProviderConfigError):
            build_provider(name, TEST_KEY, sessions, _clock())

    def test_an_unknown_provider_name_error_never_contains_the_key(self) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act
        with pytest.raises(ProviderConfigError) as raised:
            build_provider("paypal", LIVE_KEY, sessions, _clock())

        # Assert
        assert LIVE_KEY not in str(raised.value)
        assert LIVE_KEY not in repr(raised.value)

    def test_a_missing_key_error_names_no_key_material(self) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act
        with pytest.raises(ProviderConfigError) as raised:
            build_provider("stripe", None, sessions, _clock())

        # Assert
        assert "sk_" not in str(raised.value)

    def test_config_error_is_a_value_error_distinct_from_live_key_refusal(self) -> None:
        # Arrange
        sessions = _unbound_sessions()

        # Act
        with pytest.raises(ProviderConfigError) as raised:
            build_provider("stripe", None, sessions, _clock())

        # Assert
        assert isinstance(raised.value, ValueError)
        assert not isinstance(raised.value, LiveKeyRefusedError)


class TestGatewaySettingsProviderSelection:
    @staticmethod
    def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
        monkeypatch.setenv("REDIS_URL", REDIS_URL)
        monkeypatch.setenv("GATEWAY_API_KEY_PEPPER", SECRET_32)
        monkeypatch.setenv("GATEWAY_CARD_FINGERPRINT_KEY", SECRET_32)
        monkeypatch.delenv("PAYMENT_PROVIDER", raising=False)
        monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    def test_defaults_to_the_simulator_without_a_stripe_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        self._base_env(monkeypatch)

        # Act
        settings = GatewaySettings()

        # Assert
        assert settings.payment_provider == "simulator"
        assert settings.stripe_secret_key is None

    def test_reads_payment_provider_from_the_payment_provider_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        self._base_env(monkeypatch)
        monkeypatch.setenv("PAYMENT_PROVIDER", "stripe")

        # Act
        settings = GatewaySettings()

        # Assert
        assert settings.payment_provider == "stripe"

    def test_reads_the_stripe_key_as_a_secret_from_the_stripe_secret_key_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        self._base_env(monkeypatch)
        monkeypatch.setenv("STRIPE_SECRET_KEY", TEST_KEY)

        # Act
        settings = GatewaySettings()

        # Assert
        assert isinstance(settings.stripe_secret_key, SecretStr)
        assert settings.stripe_secret_key.get_secret_value() == TEST_KEY
        assert TEST_KEY not in repr(settings)
        assert TEST_KEY not in str(settings)

    def test_an_empty_stripe_secret_key_means_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Arrange
        self._base_env(monkeypatch)
        monkeypatch.setenv("STRIPE_SECRET_KEY", "")

        # Act
        settings = GatewaySettings()

        # Assert
        assert settings.stripe_secret_key is None
