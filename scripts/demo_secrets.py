"""Generates local demo secrets into a directory (a compose named volume). Never prints secret values."""

import os
import secrets
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

PEPPER = Path("gateway/gateway_api_key_pepper")
FINGERPRINT_KEY = Path("gateway/gateway_card_fingerprint_key")
PRIVATE_KEY = Path("gateway/gateway_jwt_private_key_pem")
PUBLIC_KEY = Path("ops-ai/ops_ai_jwt_public_key_pem")
PRIVATE_MODE = 0o600
PUBLIC_MODE = 0o644
SYMMETRIC_KEY_BYTES = 48


def _write_new(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
    finally:
        os.close(fd)


def _private_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def _public_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"{path.name} is not an RSA private key")
    return key


def _create_demo_secrets(root: Path) -> list[Path]:
    created: list[Path] = []
    for relative in (PEPPER, FINGERPRINT_KEY):
        if not (root / relative).exists():
            _write_new(root / relative, secrets.token_urlsafe(SYMMETRIC_KEY_BYTES).encode(), PRIVATE_MODE)
            created.append(relative)
    if (root / PRIVATE_KEY).exists():
        key = _load_private_key(root / PRIVATE_KEY)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _write_new(root / PRIVATE_KEY, _private_pem(key), PRIVATE_MODE)
        created.append(PRIVATE_KEY)
    if not (root / PUBLIC_KEY).exists():
        _write_new(root / PUBLIC_KEY, _public_pem(key), PUBLIC_MODE)
        created.append(PUBLIC_KEY)
    return created


def write_demo_secrets(root: Path) -> bool:
    """Creates only the missing files and keeps existing content. Returns True if it wrote anything.

    A missing public key is derived from the existing private key, so the pair keeps matching.
    """
    return bool(_create_demo_secrets(root))


def main() -> int:
    created = _create_demo_secrets(Path(sys.argv[1]))
    for relative in created:
        print(f"created {relative}")
    if not created:
        print("demo secrets already present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
