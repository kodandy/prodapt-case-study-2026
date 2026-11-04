"""Tests for scripts/demo_secrets.py, written from docs/specs/runtime.md (R7)."""

import os
import stat
import string
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scripts.demo_secrets import main, write_demo_secrets

PEPPER = Path("gateway/gateway_api_key_pepper")
FINGERPRINT_KEY = Path("gateway/gateway_card_fingerprint_key")
TOTP_KEY = Path("gateway/gateway_totp_encryption_key")
PRIVATE_KEY = Path("gateway/gateway_jwt_private_key_pem")
PUBLIC_KEY = Path("ops-ai/ops_ai_jwt_public_key_pem")
ALL_FILES = {PEPPER, FINGERPRINT_KEY, TOTP_KEY, PRIVATE_KEY, PUBLIC_KEY}
URL_SAFE_ALPHABET = frozenset(string.ascii_letters + string.digits + "-_")
MIN_SYMMETRIC_KEY_CHARS = 48
SCRIPT = Path(__file__).resolve().parents[1] / "demo_secrets.py"


@pytest.fixture(params=[0o000, 0o077], ids=["umask_000", "umask_077"])
def umask(request: pytest.FixtureRequest) -> Iterator[int]:
    previous = os.umask(request.param)
    yield request.param
    os.umask(previous)


def _snapshot(root: Path) -> dict[Path, bytes]:
    return {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _secret_text(root: Path, relative: Path) -> str:
    return (root / relative).read_text().strip()


def _mode(root: Path, relative: Path) -> int:
    return stat.S_IMODE((root / relative).stat().st_mode)


def _secret_fragments(root: Path) -> list[str]:
    pem_lines = [
        line
        for relative in (PRIVATE_KEY, PUBLIC_KEY)
        for line in _secret_text(root, relative).splitlines()
        if not line.startswith("-----")
    ]
    return [
        _secret_text(root, PEPPER),
        _secret_text(root, FINGERPRINT_KEY),
        _secret_text(root, TOTP_KEY),
        *pem_lines,
    ]


def test_write_demo_secrets_on_an_empty_root_returns_true_and_creates_exactly_the_five_files(
    tmp_path: Path,
) -> None:
    # Arrange: empty root

    # Act
    wrote = write_demo_secrets(tmp_path)

    # Assert
    assert wrote is True
    assert set(_snapshot(tmp_path)) == ALL_FILES


@pytest.mark.parametrize(
    "relative", [PEPPER, FINGERPRINT_KEY, TOTP_KEY], ids=["pepper", "fingerprint_key", "totp_key"]
)
def test_write_demo_secrets_symmetric_keys_are_48_or_more_url_safe_characters(
    tmp_path: Path, relative: Path
) -> None:
    # Arrange / Act
    write_demo_secrets(tmp_path)

    # Assert
    secret = _secret_text(tmp_path, relative)
    assert len(secret) >= MIN_SYMMETRIC_KEY_CHARS
    assert set(secret) <= URL_SAFE_ALPHABET


def test_write_demo_secrets_pepper_and_fingerprint_key_are_different_values(tmp_path: Path) -> None:
    # Arrange / Act
    write_demo_secrets(tmp_path)

    # Assert
    assert _secret_text(tmp_path, PEPPER) != _secret_text(tmp_path, FINGERPRINT_KEY)


def test_write_demo_secrets_private_key_is_an_rsa_2048_pkcs8_pem(tmp_path: Path) -> None:
    # Arrange / Act
    write_demo_secrets(tmp_path)

    # Assert
    pem = (tmp_path / PRIVATE_KEY).read_bytes()
    private_key = serialization.load_pem_private_key(pem, password=None)
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----")
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert private_key.key_size == 2048


def test_write_demo_secrets_public_key_is_the_public_half_of_the_private_key(tmp_path: Path) -> None:
    # Arrange / Act
    write_demo_secrets(tmp_path)

    # Assert
    private_key = serialization.load_pem_private_key((tmp_path / PRIVATE_KEY).read_bytes(), password=None)
    public_key = serialization.load_pem_public_key((tmp_path / PUBLIC_KEY).read_bytes())
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert public_key.public_numbers() == private_key.public_key().public_numbers()


@pytest.mark.usefixtures("umask")
def test_write_demo_secrets_sets_private_files_to_0600_and_the_public_key_to_0644(tmp_path: Path) -> None:
    # Arrange: the umask fixture runs the test under a permissive and a restrictive umask

    # Act
    write_demo_secrets(tmp_path)

    # Assert
    assert {relative: _mode(tmp_path, relative) for relative in ALL_FILES} == {
        PEPPER: 0o600,
        FINGERPRINT_KEY: 0o600,
        TOTP_KEY: 0o600,
        PRIVATE_KEY: 0o600,
        PUBLIC_KEY: 0o644,
    }


def test_write_demo_secrets_second_call_returns_false_and_changes_no_byte(tmp_path: Path) -> None:
    # Arrange
    write_demo_secrets(tmp_path)
    before = _snapshot(tmp_path)

    # Act
    wrote = write_demo_secrets(tmp_path)

    # Assert
    assert wrote is False
    assert _snapshot(tmp_path) == before


@pytest.mark.parametrize(
    "missing",
    [PEPPER, FINGERPRINT_KEY, TOTP_KEY, PUBLIC_KEY],
    ids=["pepper", "fingerprint_key", "totp_key", "public_key"],
)
def test_write_demo_secrets_recreates_only_the_missing_file_and_keeps_the_others(
    tmp_path: Path, missing: Path
) -> None:
    # Arrange
    write_demo_secrets(tmp_path)
    before = _snapshot(tmp_path)
    (tmp_path / missing).unlink()

    # Act
    wrote = write_demo_secrets(tmp_path)

    # Assert
    after = _snapshot(tmp_path)
    assert wrote is True
    assert set(after) == ALL_FILES
    assert {path: data for path, data in after.items() if path != missing} == {
        path: data for path, data in before.items() if path != missing
    }


def test_write_demo_secrets_regenerated_public_key_still_matches_the_existing_private_key(
    tmp_path: Path,
) -> None:
    # Arrange
    write_demo_secrets(tmp_path)
    (tmp_path / PUBLIC_KEY).unlink()

    # Act
    write_demo_secrets(tmp_path)

    # Assert
    private_key = serialization.load_pem_private_key((tmp_path / PRIVATE_KEY).read_bytes(), password=None)
    public_key = serialization.load_pem_public_key((tmp_path / PUBLIC_KEY).read_bytes())
    assert isinstance(private_key, rsa.RSAPrivateKey)
    assert isinstance(public_key, rsa.RSAPublicKey)
    assert public_key.public_numbers() == private_key.public_key().public_numbers()


def test_write_demo_secrets_keeps_the_content_of_a_pre_existing_file(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / PEPPER).parent.mkdir(parents=True)
    (tmp_path / PEPPER).write_text("operator-chosen-pepper")

    # Act
    wrote = write_demo_secrets(tmp_path)

    # Assert
    assert wrote is True
    assert (tmp_path / PEPPER).read_text() == "operator-chosen-pepper"
    assert set(_snapshot(tmp_path)) == ALL_FILES


def test_write_demo_secrets_gives_two_different_roots_different_secrets(tmp_path: Path) -> None:
    # Arrange
    first, second = tmp_path / "first", tmp_path / "second"

    # Act
    write_demo_secrets(first)
    write_demo_secrets(second)

    # Assert
    for relative in (PEPPER, FINGERPRINT_KEY, TOTP_KEY, PRIVATE_KEY, PUBLIC_KEY):
        assert (first / relative).read_bytes() != (second / relative).read_bytes()


def test_write_demo_secrets_prints_nothing_that_contains_a_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Arrange / Act
    write_demo_secrets(tmp_path)

    # Assert
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert [fragment for fragment in _secret_fragments(tmp_path) if fragment in output] == []


def test_main_prints_only_the_created_file_names_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    monkeypatch.setattr(sys, "argv", ["demo_secrets.py", str(tmp_path)])

    # Act
    exit_code = main()

    # Assert
    output = capsys.readouterr().out
    assert exit_code == 0
    assert [relative.name for relative in ALL_FILES if relative.name not in output] == []
    assert [fragment for fragment in _secret_fragments(tmp_path) if fragment in output] == []


def test_main_on_a_populated_root_exits_zero_and_names_no_created_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange
    write_demo_secrets(tmp_path)
    before = _snapshot(tmp_path)
    monkeypatch.setattr(sys, "argv", ["demo_secrets.py", str(tmp_path)])

    # Act
    exit_code = main()

    # Assert
    output = capsys.readouterr().out
    assert exit_code == 0
    assert [relative.name for relative in ALL_FILES if relative.name in output] == []
    assert _snapshot(tmp_path) == before


def test_script_run_as_a_command_writes_the_secrets_into_the_given_directory(tmp_path: Path) -> None:
    # Arrange
    target = tmp_path / "demo-secrets"

    # Act
    result = subprocess.run(  # noqa: S603 - fixed argv built from sys.executable
        [sys.executable, str(SCRIPT), str(target)], capture_output=True, text=True, check=False, timeout=60
    )

    # Assert
    assert result.returncode == 0
    assert set(_snapshot(target)) == ALL_FILES
    assert [
        fragment for fragment in _secret_fragments(target) if fragment in result.stdout + result.stderr
    ] == []
