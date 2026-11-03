from pathlib import Path

import pytest
from gateway.config import GatewaySettings
from pydantic import SecretStr, ValidationError

SECRET_32 = "k" * 32
DATABASE_URL = "postgresql+psycopg://user:pw@localhost:5432/db"
REDIS_URL = "redis://localhost:6379/0"


def _set_env(monkeypatch: pytest.MonkeyPatch, pepper: str, fingerprint_key: str) -> None:
    monkeypatch.setenv("DATABASE_URL", DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REDIS_URL)
    monkeypatch.setenv("GATEWAY_API_KEY_PEPPER", pepper)
    monkeypatch.setenv("GATEWAY_CARD_FINGERPRINT_KEY", fingerprint_key)


def _error_summary(exc: pytest.ExceptionInfo[ValidationError]) -> list[tuple[tuple[int | str, ...], str]]:
    return [(error["loc"], error["type"]) for error in exc.value.errors()]


def test_gateway_settings_accepts_secrets_of_exactly_32_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _set_env(monkeypatch, pepper="p" * 32, fingerprint_key="f" * 32)

    # Act
    settings = GatewaySettings()

    # Assert
    assert settings.api_key_pepper.get_secret_value() == "p" * 32
    assert settings.card_fingerprint_key.get_secret_value() == "f" * 32
    assert settings.database_url == DATABASE_URL
    assert settings.redis_url == REDIS_URL


@pytest.mark.parametrize("length", [0, 1, 31])
def test_gateway_settings_rejects_an_api_key_pepper_shorter_than_32_characters(
    monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    # Arrange
    _set_env(monkeypatch, pepper="p" * length, fingerprint_key=SECRET_32)

    # Act
    with pytest.raises(ValidationError) as exc:
        GatewaySettings()

    # Assert
    assert _error_summary(exc) == [(("api_key_pepper",), "too_short")]


@pytest.mark.parametrize("length", [0, 1, 31])
def test_gateway_settings_rejects_a_card_fingerprint_key_shorter_than_32_characters(
    monkeypatch: pytest.MonkeyPatch, length: int
) -> None:
    # Arrange
    _set_env(monkeypatch, pepper=SECRET_32, fingerprint_key="f" * length)

    # Act
    with pytest.raises(ValidationError) as exc:
        GatewaySettings()

    # Assert
    assert _error_summary(exc) == [(("card_fingerprint_key",), "too_short")]


def test_gateway_settings_reports_both_secrets_when_both_are_too_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    _set_env(monkeypatch, pepper="p" * 31, fingerprint_key="f" * 31)

    # Act
    with pytest.raises(ValidationError) as exc:
        GatewaySettings()

    # Assert
    assert sorted(_error_summary(exc)) == [
        (("api_key_pepper",), "too_short"),
        (("card_fingerprint_key",), "too_short"),
    ]


@pytest.mark.parametrize(
    ("pepper", "fingerprint_key", "leaked"),
    [
        ("leaky-pepper-9f3a-short", SECRET_32, "leaky-pepper-9f3a-short"),
        (SECRET_32, "leaky-fingerprint-7c1d-short", "leaky-fingerprint-7c1d-short"),
    ],
    ids=["pepper", "fingerprint_key"],
)
def test_gateway_settings_validation_error_does_not_contain_the_rejected_secret(
    monkeypatch: pytest.MonkeyPatch, pepper: str, fingerprint_key: str, leaked: str
) -> None:
    # Arrange
    _set_env(monkeypatch, pepper=pepper, fingerprint_key=fingerprint_key)

    # Act
    with pytest.raises(ValidationError) as exc:
        GatewaySettings()

    # Assert
    assert leaked not in str(exc.value)
    assert leaked not in repr(exc.value)


# --- R3: jwt_private_key_pem ---


def _reveal(secret: SecretStr | None) -> str | None:
    return None if secret is None else secret.get_secret_value()


PEM_VALUE = "-----BEGIN PRIVATE KEY-----\nTEST-KEY-BODY-NOT-A-SECRET\n-----END PRIVATE KEY-----"


def test_gateway_settings_jwt_private_key_pem_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _set_env(monkeypatch, pepper=SECRET_32, fingerprint_key=SECRET_32)
    monkeypatch.delenv("GATEWAY_JWT_PRIVATE_KEY_PEM", raising=False)

    # Act
    settings = GatewaySettings()

    # Assert
    assert settings.jwt_private_key_pem is None


def test_gateway_settings_reads_jwt_private_key_pem_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrange
    _set_env(monkeypatch, pepper=SECRET_32, fingerprint_key=SECRET_32)
    monkeypatch.setenv("GATEWAY_JWT_PRIVATE_KEY_PEM", PEM_VALUE)

    # Act
    settings = GatewaySettings()

    # Assert
    assert _reveal(settings.jwt_private_key_pem) == PEM_VALUE


def test_gateway_settings_reads_jwt_private_key_pem_from_the_secrets_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Arrange
    _set_env(monkeypatch, pepper=SECRET_32, fingerprint_key=SECRET_32)
    monkeypatch.delenv("GATEWAY_JWT_PRIVATE_KEY_PEM", raising=False)
    (tmp_path / "gateway_jwt_private_key_pem").write_text(PEM_VALUE)

    # Act
    settings = GatewaySettings(_secrets_dir=tmp_path)

    # Assert
    assert _reveal(settings.jwt_private_key_pem) == PEM_VALUE


def test_gateway_settings_repr_does_not_contain_the_jwt_private_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Arrange
    _set_env(monkeypatch, pepper=SECRET_32, fingerprint_key=SECRET_32)
    monkeypatch.setenv("GATEWAY_JWT_PRIVATE_KEY_PEM", PEM_VALUE)

    # Act
    settings = GatewaySettings()

    # Assert
    assert "TEST-KEY-BODY-NOT-A-SECRET" not in repr(settings)
    assert "TEST-KEY-BODY-NOT-A-SECRET" not in str(settings)
