"""B5.2: every claim cites evidence of this turn, or it is dropped. No evidence at all means abstain."""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ops_ai.assistant.evidence import EvidenceItem

ABSTAIN_TEXT = "Not enough data to answer this from the records available."

_CITATION = re.compile(r"\[([^\[\]]+)\]")


@dataclass(frozen=True)
class Claim:
    text: str
    evidence_refs: tuple[str, ...]


def ground_claims(claims: Iterable[Claim], evidence: Sequence[EvidenceItem]) -> tuple[Claim, ...]:
    known = {item.ref for item in evidence}
    grounded: list[Claim] = []
    for claim in claims:
        refs = tuple(dict.fromkeys(claim.evidence_refs))
        if refs and all(ref in known for ref in refs):
            grounded.append(Claim(text=claim.text, evidence_refs=refs))
    return tuple(grounded)


def cited_refs(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(_CITATION.findall(text)))


def grounded_narration(text: str, evidence: Sequence[EvidenceItem]) -> str | None:
    """The text only when it cites at least one ref and every cited ref is evidence of this turn."""
    refs = cited_refs(text)
    known = {item.ref for item in evidence}
    if refs and all(ref in known for ref in refs):
        return text
    return None
