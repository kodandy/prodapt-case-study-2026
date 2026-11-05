"""Deterministic token counter: word runs and single punctuation characters (no tokenizer dependency)."""

import re

_TOKEN = re.compile(r"\w+|[^\w\s]")


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))
