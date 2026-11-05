"""B4.3: the versioned failure playbook. Suggested fixes come from this file, never from free LLM text."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from ops_ai.investigation.reasons import TAXONOMY

DEFAULT_PLAYBOOK_PATH = Path(__file__).resolve().parent / "playbooks" / "failures.v1.json"


@dataclass(frozen=True)
class PlaybookEntry:
    taxonomy_code: str
    fix: str
    hypothesis: str
    how_to_confirm: tuple[str, ...]


@dataclass(frozen=True)
class Playbook:
    version: str
    entries: Mapping[str, PlaybookEntry]


class _EntryFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    taxonomy_code: str
    fix: str = Field(min_length=1)
    hypothesis: str = Field(min_length=1)
    how_to_confirm: list[str] = Field(min_length=1)


class _PlaybookFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: str = Field(min_length=1)
    entries: list[_EntryFile]


def load_playbook(path: Path = DEFAULT_PLAYBOOK_PATH) -> Playbook:
    """Raises ValueError for malformed files, unknown or duplicated codes, and any taxonomy code left out."""
    parsed = _PlaybookFile.model_validate(json.loads(path.read_text(encoding="utf-8")))
    entries: dict[str, PlaybookEntry] = {}
    for item in parsed.entries:
        if item.taxonomy_code not in TAXONOMY:
            raise ValueError(f"unknown taxonomy code in playbook: {item.taxonomy_code}")
        if item.taxonomy_code in entries:
            raise ValueError(f"duplicated taxonomy code in playbook: {item.taxonomy_code}")
        entries[item.taxonomy_code] = PlaybookEntry(
            taxonomy_code=item.taxonomy_code,
            fix=item.fix,
            hypothesis=item.hypothesis,
            how_to_confirm=tuple(item.how_to_confirm),
        )
    missing = sorted(set(TAXONOMY) - set(entries))
    if missing:
        raise ValueError(f"playbook lacks entries for: {', '.join(missing)}")
    return Playbook(version=parsed.version, entries=MappingProxyType(entries))
