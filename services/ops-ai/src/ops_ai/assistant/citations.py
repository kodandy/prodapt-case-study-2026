"""R1-5: per-sentence citations for `/v1/assistant/ask?grounded=true` (docs/specs/sq1.md, "Grounded /ask").

Deterministic and LLM-agnostic: relevance is decided before the LLM is called, and the LLM text is checked
sentence by sentence against the evidence it was given.
"""

import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Literal

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import cited_refs

CANNOT_ANSWER_TEXT = "I cannot answer this from the available sources."
EXCERPT_CHARS = 160
MIN_TERM_CHARS = 3
MIN_QUOTE_CHARS = 12
"""An uncited sentence of at least this many characters that appears verbatim in one evidence summary is
attributed to that evidence (a quote is grounded by the chunk it quotes)."""
STOPWORDS = frozenset(
    {
        "about", "all", "and", "any", "are", "can", "did", "does", "for", "from", "get", "give", "has",
        "have", "how", "into", "its", "list", "many", "me", "much", "my", "not", "our", "please", "show",
        "some", "tell", "than", "that", "the", "their", "them", "then", "there", "these", "this", "was",
        "were", "what", "when", "where", "which", "who", "why", "will", "with", "would", "you", "your",
    }
)  # fmt: skip

UnsupportedReason = Literal["no_citation", "unknown_ref"]

_WORD = re.compile(r"[A-Za-z0-9]+")
_CITATION_GROUP = re.compile(r"\[[^\[\]]+\]")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


@dataclass(frozen=True)
class SourceExcerpt:
    ref: str
    excerpt: str
    """A verbatim substring of the evidence summary (the chunk text), at most EXCERPT_CHARS long."""


@dataclass(frozen=True)
class CitedSentence:
    sentence: str
    sources: tuple[SourceExcerpt, ...]


@dataclass(frozen=True)
class UnsupportedSentence:
    sentence: str
    reason: UnsupportedReason
    refs: tuple[str, ...]
    """The refs the sentence cited; for unknown_ref at least one of them is not evidence of this turn."""


@dataclass(frozen=True)
class Grounding:
    text: str
    """The supported sentences only, one per line; CANNOT_ANSWER_TEXT when none is supported."""
    grounded: bool
    citations: tuple[CitedSentence, ...]
    unsupported: tuple[UnsupportedSentence, ...]


def _norm(word: str) -> str:
    lowered = word.lower()
    return lowered[:-1] if len(lowered) > MIN_TERM_CHARS and lowered.endswith("s") else lowered


def terms(text: str) -> frozenset[str]:
    """Lower-cased alphanumeric words of at least MIN_TERM_CHARS characters, a plural `s` removed, minus
    STOPWORDS. `card_declined` yields `card` and `declined`."""
    words = (_norm(match.group(0)) for match in _WORD.finditer(text))
    return frozenset(word for word in words if len(word) >= MIN_TERM_CHARS and word not in STOPWORDS)


def relevant_evidence(
    evidence: Sequence[EvidenceItem], question: str, vector_refs: Collection[str]
) -> tuple[EvidenceItem, ...]:
    """Evidence the question can be answered from: a vector-leg hit at or above DEFAULT_MIN_SIMILARITY (its
    ref is in `vector_refs`), or an item whose ref or summary shares at least one term with the question."""
    wanted = terms(question)
    return tuple(
        item for item in evidence if item.ref in vector_refs or wanted & terms(f"{item.ref} {item.summary}")
    )


def split_sentences(text: str) -> list[str]:
    """Lines, then sentences ending in `.`, `!` or `?`. A fragment holding only citations (`fact. [ref]`) is
    joined to the sentence before it."""
    sentences: list[str] = []
    for line in text.splitlines():
        for part in _SENTENCE_END.split(line.strip()):
            fragment = part.strip()
            if not fragment:
                continue
            if sentences and not _CITATION_GROUP.sub("", fragment).strip(" .!?"):
                sentences[-1] = f"{sentences[-1]} {fragment}"
            else:
                sentences.append(fragment)
    return sentences


def excerpt_for(sentence: str, summary: str) -> str:
    """Starts at the first summary word the sentence also uses (else at the start), up to EXCERPT_CHARS, cut
    back to a word boundary. Always a verbatim substring of `summary`."""
    wanted = terms(_CITATION_GROUP.sub(" ", sentence))
    start = 0
    for match in _WORD.finditer(summary):
        if _norm(match.group(0)) in wanted:
            start = match.start()
            break
    window = summary[start : start + EXCERPT_CHARS]
    if start + EXCERPT_CHARS < len(summary):
        cut = window.rfind(" ")
        if cut > 0:
            window = window[:cut]
    return window.strip()


def _quoted_source(sentence: str, evidence: Sequence[EvidenceItem]) -> EvidenceItem | None:
    quote = " ".join(_BULLET.sub("", sentence).split())
    if len(quote) < MIN_QUOTE_CHARS:
        return None
    for item in evidence:
        if quote in " ".join(item.summary.split()):
            return item
    return None


def ground_answer(text: str, evidence: Sequence[EvidenceItem]) -> Grounding:
    """Every sentence must cite refs of `evidence` only (or quote one summary verbatim). A sentence citing any
    other ref is flagged unknown_ref and dropped; an uncited sentence is flagged no_citation and dropped."""
    by_ref = {item.ref: item for item in evidence}
    citations: list[CitedSentence] = []
    unsupported: list[UnsupportedSentence] = []
    for sentence in split_sentences(text):
        refs = cited_refs(sentence)
        if not refs:
            quoted = _quoted_source(sentence, evidence)
            if quoted is None:
                unsupported.append(UnsupportedSentence(sentence=sentence, reason="no_citation", refs=()))
            else:
                source = SourceExcerpt(ref=quoted.ref, excerpt=excerpt_for(sentence, quoted.summary))
                citations.append(CitedSentence(sentence=sentence, sources=(source,)))
            continue
        if any(ref not in by_ref for ref in refs):
            unsupported.append(UnsupportedSentence(sentence=sentence, reason="unknown_ref", refs=refs))
            continue
        sources = tuple(
            SourceExcerpt(ref=ref, excerpt=excerpt_for(sentence, by_ref[ref].summary)) for ref in refs
        )
        citations.append(CitedSentence(sentence=sentence, sources=sources))
    if not citations:
        return Grounding(
            text=CANNOT_ANSWER_TEXT, grounded=False, citations=(), unsupported=tuple(unsupported)
        )
    return Grounding(
        text="\n".join(cited.sentence for cited in citations),
        grounded=True,
        citations=tuple(citations),
        unsupported=tuple(unsupported),
    )


def cannot_answer() -> Grounding:
    return Grounding(text=CANNOT_ANSWER_TEXT, grounded=False, citations=(), unsupported=())
