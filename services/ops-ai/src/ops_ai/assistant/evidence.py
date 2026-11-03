from dataclasses import dataclass


@dataclass(frozen=True)
class EvidenceItem:
    ref: str
    """`<kind>:<id>` for documents, `aggregate:decline_code:<code>` for aggregates."""
    summary: str
