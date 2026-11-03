import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    """Built from auth only, never from model output. Every repository function takes one."""

    merchant_id: uuid.UUID
