"""Short-term conversation state (B10.2): pure rules, no I/O. Merchant scope is never part of the text."""

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

Metric = Literal["failure_rate", "payment_volume"]
ChatIntent = Literal["metric_query", "memory_proposal", "unsupported"]

MAX_MEMORY_CHARS = 500
DEFAULT_WINDOW = timedelta(hours=24)
MAX_HOURS = 720
MAX_DAYS = 90

_FAILURE_WORDS = ("fail", "declin", "error")
_VOLUME_PHRASES = ("volume", "how many payments", "number of payments", "payment count")
_LAST_N = re.compile(r"\b(?:last|past)\s+([0-9]{1,4})\s+(hour|day)s?\b")
_MEMORY_PREFIXES = ("remember that ", "remember: ")
# control, format (ZWSP, RLO), line/paragraph separators, private use, surrogates
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cs"})
_DIGIT_RUN = re.compile(r"\d(?:[^0-9A-Za-z]{0,3}\d)+")


@dataclass(frozen=True)
class TimeWindow:
    """`[start, end)` in UTC."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class ConversationState:
    metric: Metric | None
    window: TimeWindow | None


EMPTY_STATE = ConversationState(metric=None, window=None)


def detect_metric(question: str) -> Metric | None:
    lowered = question.lower()
    if any(word in lowered for word in _FAILURE_WORDS):
        return "failure_rate"
    if any(phrase in lowered for phrase in _VOLUME_PHRASES):
        return "payment_volume"
    return None


def detect_window(question: str, now: datetime) -> TimeWindow | None:
    lowered = question.lower()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in lowered:
        return TimeWindow(start=midnight - timedelta(days=1), end=midnight)
    if "today" in lowered:
        return TimeWindow(start=midnight, end=now)
    match = _LAST_N.search(lowered)
    if match is not None:
        count, unit = int(match.group(1)), match.group(2)
        if unit == "hour" and 1 <= count <= MAX_HOURS:
            return TimeWindow(start=now - timedelta(hours=count), end=now)
        if unit == "day" and 1 <= count <= MAX_DAYS:
            return TimeWindow(start=now - timedelta(days=count), end=now)
        return None
    if "last week" in lowered:
        return TimeWindow(start=now - timedelta(days=7), end=now)
    return None


def is_storable_memory(content: str) -> bool:
    """1..MAX_MEMORY_CHARS, no hidden characters, no card number (long-term rows outlive any masking)."""
    if not content or len(content) > MAX_MEMORY_CHARS:
        return False
    if any(unicodedata.category(ch) in _HIDDEN_CATEGORIES for ch in content):
        return False
    return not _holds_card_number(unicodedata.normalize("NFKC", content))


def _holds_card_number(text: str) -> bool:
    """Any run of 13+ digits joined by up to 3 non-alphanumerics (stricter than the prompt masking)."""
    return any(len(re.sub(r"\D", "", run.group())) >= 13 for run in _DIGIT_RUN.finditer(text))


def detect_memory_request(question: str) -> str | None:
    stripped = question.strip()
    lowered = stripped.lower()
    for prefix in _MEMORY_PREFIXES:
        if lowered.startswith(prefix):
            content = stripped[len(prefix) :].strip()
            if not is_storable_memory(content):
                return None
            return content
    return None


def resolve(
    question: str, previous: ConversationState, now: datetime
) -> tuple[ChatIntent, ConversationState]:
    """A follow-up without a metric keeps the previous one; a new time phrase moves the window."""
    if detect_memory_request(question) is not None:
        return "memory_proposal", previous
    metric = detect_metric(question) or previous.metric
    if metric is None:
        return "unsupported", previous
    window = (
        detect_window(question, now) or previous.window or TimeWindow(start=now - DEFAULT_WINDOW, end=now)
    )
    return "metric_query", ConversationState(metric=metric, window=window)
