"""Tests for ops_ai/investigation/playbook.py, written from docs/specs/failure-tools.md section 7 (loader)."""

import json
from pathlib import Path

import pytest
from ops_ai.investigation.playbook import DEFAULT_PLAYBOOK_PATH, Playbook, PlaybookEntry, load_playbook
from ops_ai.investigation.reasons import TAXONOMY


def _entry(code: str) -> dict[str, object]:
    return {
        "taxonomy_code": code,
        "fix": f"Fix for {code}",
        "hypothesis": f"Hypothesis for {code}",
        "how_to_confirm": [f"Check A for {code}", f"Check B for {code}"],
    }


def _payload() -> dict[str, object]:
    return {"version": "test.v1", "entries": [_entry(code) for code in TAXONOMY]}


def _write(tmp_path: Path, content: object) -> Path:
    path = tmp_path / "playbook.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    return path


def _entries(payload: dict[str, object]) -> list[dict[str, object]]:
    entries = payload["entries"]
    assert isinstance(entries, list)
    return entries


# --- the shipped playbook ---------------------------------------------------------------------------------


def test_default_playbook_path_points_at_the_shipped_failures_v1_file() -> None:
    # Arrange / Act
    path = DEFAULT_PLAYBOOK_PATH

    # Assert
    assert (path.parent.name, path.name, path.is_file()) == ("playbooks", "failures.v1.json", True)


def test_shipped_playbook_loads_with_a_non_empty_version() -> None:
    # Arrange / Act
    playbook = load_playbook()

    # Assert
    assert isinstance(playbook, Playbook)
    assert playbook.version.strip() != ""


def test_loading_the_default_path_explicitly_gives_the_same_playbook() -> None:
    # Arrange / Act
    explicit = load_playbook(DEFAULT_PLAYBOOK_PATH)

    # Assert
    assert explicit == load_playbook()


def test_shipped_playbook_covers_every_taxonomy_code_and_nothing_else() -> None:
    # Arrange / Act
    playbook = load_playbook()

    # Assert
    assert sorted(playbook.entries) == sorted(TAXONOMY)


@pytest.mark.parametrize("code", sorted(TAXONOMY))
def test_shipped_playbook_entry_has_a_fix_a_hypothesis_and_at_least_one_confirmation_step(code: str) -> None:
    # Arrange
    playbook = load_playbook()

    # Act
    entry = playbook.entries[code]

    # Assert
    assert isinstance(entry, PlaybookEntry)
    assert entry.taxonomy_code == code
    assert entry.fix.strip() != ""
    assert entry.hypothesis.strip() != ""
    assert len(entry.how_to_confirm) >= 1
    assert [step for step in entry.how_to_confirm if step.strip() == ""] == []


# --- loading a custom file --------------------------------------------------------------------------------


def test_load_playbook_reads_version_and_entries_from_the_given_file(tmp_path: Path) -> None:
    # Arrange
    path = _write(tmp_path, _payload())

    # Act
    playbook = load_playbook(path)

    # Assert
    assert playbook.version == "test.v1"
    assert playbook.entries["rate_limited"] == PlaybookEntry(
        taxonomy_code="rate_limited",
        fix="Fix for rate_limited",
        hypothesis="Hypothesis for rate_limited",
        how_to_confirm=("Check A for rate_limited", "Check B for rate_limited"),
    )
    assert sorted(playbook.entries) == sorted(TAXONOMY)


# --- load_playbook: ValueError cases ----------------------------------------------------------------------


def test_load_playbook_rejects_malformed_json(tmp_path: Path) -> None:
    # Arrange
    path = _write(tmp_path, '{"version": "test.v1", "entries": [')

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        '"playbook"',
        "42",
        "null",
        "{}",
        '{"version": "test.v1"}',
        '{"version": "test.v1", "entries": {}}',
    ],
    ids=["array", "string", "number", "null", "empty_object", "missing_entries", "entries_not_a_list"],
)
def test_load_playbook_rejects_a_file_with_the_wrong_shape(tmp_path: Path, content: str) -> None:
    # Arrange
    path = _write(tmp_path, content)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


def test_load_playbook_rejects_an_entry_without_a_fix(tmp_path: Path) -> None:
    # Arrange
    payload = _payload()
    del _entries(payload)[0]["fix"]
    path = _write(tmp_path, payload)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


def test_load_playbook_rejects_confirmation_steps_that_are_not_a_list_of_strings(tmp_path: Path) -> None:
    # Arrange
    payload = _payload()
    _entries(payload)[0]["how_to_confirm"] = "just check it"
    path = _write(tmp_path, payload)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


def test_load_playbook_rejects_an_unknown_taxonomy_code(tmp_path: Path) -> None:
    # Arrange
    payload = _payload()
    _entries(payload).append(_entry("made_up_code"))
    path = _write(tmp_path, payload)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


def test_load_playbook_rejects_a_duplicated_taxonomy_code(tmp_path: Path) -> None:
    # Arrange
    payload = _payload()
    _entries(payload).append(_entry("processing_error"))
    path = _write(tmp_path, payload)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


@pytest.mark.parametrize("missing", sorted(TAXONOMY))
def test_load_playbook_rejects_a_file_missing_any_taxonomy_code(tmp_path: Path, missing: str) -> None:
    # Arrange
    payload = _payload()
    payload["entries"] = [entry for entry in _entries(payload) if entry["taxonomy_code"] != missing]
    path = _write(tmp_path, payload)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)


def test_load_playbook_rejects_a_file_without_a_version(tmp_path: Path) -> None:
    # Arrange
    payload = _payload()
    del payload["version"]
    path = _write(tmp_path, payload)

    # Act / Assert
    with pytest.raises(ValueError):
        load_playbook(path)
