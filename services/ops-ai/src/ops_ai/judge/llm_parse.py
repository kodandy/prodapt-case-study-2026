"""Schema validation of a judge LLM's JSON reply (docs/specs/evaluation-judge.md section 6)."""

import json
import re

from pydantic import BaseModel, ValidationError

MAX_REPLY_BYTES = 8 * 1024
_FENCE = re.compile(r"^```(?:json)?\s*\n(?P<body>.*?)\n?```$", re.DOTALL)


def parse_reply[M: BaseModel](text: str, schema: type[M]) -> M | None:
    """The reply as `schema`, or None when it is too large, not JSON or off-schema. Fences are allowed."""
    try:
        if len(text.encode("utf-8")) > MAX_REPLY_BYTES:
            return None
        stripped = text.strip()
        fenced = _FENCE.match(stripped)
        if fenced is not None:
            stripped = fenced.group("body").strip()
        return schema.model_validate(json.loads(stripped))
    except (ValidationError, ValueError, RecursionError, UnicodeEncodeError):
        return None
