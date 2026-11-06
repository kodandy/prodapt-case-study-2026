"""Opaque keyset cursors for the fraud-rule history and audit (docs/specs/r2-agents.md section 7.1).

A cursor names the version to continue below and is bound to its endpoint kind and to the token merchant,
so it can never select another merchant's rows or be replayed against the other route.
"""

import base64
import binascii
import re
import uuid
from typing import Literal

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_CURSOR_LENGTH = 256
MAX_VERSION = 2**31 - 1

CursorKind = Literal["history", "audit"]

_VERSION = re.compile(r"[1-9][0-9]*")


class InvalidCursorError(Exception):
    pass


def encode_cursor(kind: CursorKind, merchant_id: uuid.UUID, version: int) -> str:
    raw = f"{kind}:{merchant_id}:{version}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, kind: CursorKind, merchant_id: uuid.UUID) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True).decode("ascii")
    except (binascii.Error, UnicodeError, ValueError):
        raise InvalidCursorError from None
    parts = raw.split(":")
    if len(parts) != 3:
        raise InvalidCursorError
    cursor_kind, cursor_merchant, version_text = parts
    if cursor_kind != kind or cursor_merchant != str(merchant_id) or not _VERSION.fullmatch(version_text):
        raise InvalidCursorError
    version = int(version_text)
    if version > MAX_VERSION or encode_cursor(kind, merchant_id, version) != cursor:
        raise InvalidCursorError
    return version
