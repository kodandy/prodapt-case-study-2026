"""The dataset committed under data/synthetic/ is exactly what the generator writes
for seed 42 and 12000 records."""

from __future__ import annotations

from pathlib import Path

from scripts.generate_dataset import generate, write_dataset

COMMITTED_DIR = Path(__file__).resolve().parents[2] / "data" / "synthetic"


def test_committed_transactions_csv_matches_generator_output(tmp_path: Path) -> None:
    # Arrange
    files = write_dataset(generate(seed=42, records=12_000), tmp_path)

    # Act
    committed = (COMMITTED_DIR / "transactions.csv").read_bytes()

    # Assert
    assert committed == files.csv_path.read_bytes()


def test_committed_incidents_json_matches_generator_output(tmp_path: Path) -> None:
    # Arrange
    files = write_dataset(generate(seed=42, records=12_000), tmp_path)

    # Act
    committed = (COMMITTED_DIR / "incidents.json").read_bytes()

    # Assert
    assert committed == files.incidents_path.read_bytes()


def test_committed_transactions_csv_holds_header_plus_12000_records() -> None:
    # Arrange
    path = COMMITTED_DIR / "transactions.csv"

    # Act
    lines = path.read_text(encoding="utf-8").splitlines()

    # Assert
    assert len(lines) == 12_001
