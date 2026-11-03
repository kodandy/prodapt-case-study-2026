"""Tests for scripts/generate_dataset.py, written from docs/specs/dataset.md (D1-D3, SQ1-SQ4)."""

from __future__ import annotations

import csv
import dataclasses
import inspect
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from scripts.generate_dataset import (
    BRIEF_FIELDS,
    DEMO_DAY,
    EXPORT_FIELDS,
    EXTENSION_FIELDS,
    CanonicalTransaction,
    DatasetFiles,
    Incident,
    ScenarioLog,
    generate,
    main,
    to_export_row,
    write_dataset,
)

Row = dict[str, Any]

DEFAULT_RECORDS = 12_000
SMALL_RECORDS = 2_000
OCTOBER_START = datetime(2026, 10, 1, tzinfo=UTC)
NOVEMBER_START = datetime(2026, 11, 1, tzinfo=UTC)
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
PAN_LIKE = re.compile(r"\d{13,19}")

ENUMS: dict[str, set[str]] = {
    "payment_method": {"card", "bank_transfer", "wallet", "upi"},
    "status": {"success", "failed", "pending", "flagged", "reversed"},
    "settlement_status": {"settled", "pending", "disputed", "reversed"},
    "kyc_status": {"verified", "pending", "failed"},
    "currency": {"USD", "EUR", "INR", "JPY"},
    "record_kind": {"merchant_payment", "p2p_transfer"},
}
FULL_COVERAGE_FIELDS = ("payment_method", "status", "settlement_status", "kyc_status")


def _ts(row: Row) -> datetime:
    parsed = datetime.fromisoformat(str(row["timestamp"]))
    assert parsed.tzinfo is not None
    return parsed


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _failure_rate(rows: list[Row]) -> float:
    return sum(1 for r in rows if r["status"] == "failed") / len(rows)


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        return fieldnames, list(reader)


def _coerce_csv_row(csv_row: dict[str, str], json_row: Row) -> Row:
    """Coerce a CSV row using the JSON value's type as the type oracle."""
    coerced: Row = {}
    for field in EXPORT_FIELDS:
        raw = csv_row[field]
        expected = json_row[field]
        if expected is None:
            coerced[field] = None if raw == "" else raw
        elif isinstance(expected, bool):
            coerced[field] = {"true": True, "false": False}.get(raw, raw)
        elif isinstance(expected, int | float):
            coerced[field] = float(raw)
        else:
            coerced[field] = raw
    return coerced


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate()


@pytest.fixture(scope="module")
def rows(log: ScenarioLog) -> list[Row]:
    return [to_export_row(tx) for tx in log.transactions]


@pytest.fixture(scope="module")
def rows_by_id(rows: list[Row]) -> dict[str, Row]:
    return {str(r["transaction_id"]): r for r in rows}


@pytest.fixture(scope="module")
def small_log() -> ScenarioLog:
    return generate(seed=7, records=SMALL_RECORDS)


@pytest.fixture(scope="module")
def written(small_log: ScenarioLog, tmp_path_factory: pytest.TempPathFactory) -> DatasetFiles:
    return write_dataset(small_log, tmp_path_factory.mktemp("dataset_written"))


# --------------------------------------------------------------------------- D1: reproducibility


class TestReproducibility:
    def test_default_arguments_are_seed_42_and_12000_records(self) -> None:
        # Arrange / Act
        params = inspect.signature(generate).parameters

        # Assert
        assert params["seed"].default == 42
        assert params["records"].default == DEFAULT_RECORDS

    def test_default_dataset_has_12000_transactions(self, log: ScenarioLog) -> None:
        # Arrange / Act
        count = len(log.transactions)

        # Assert
        assert count == DEFAULT_RECORDS

    def test_requested_record_count_is_honoured(self, small_log: ScenarioLog) -> None:
        # Arrange / Act
        count = len(small_log.transactions)

        # Assert
        assert count == SMALL_RECORDS

    def test_minimum_record_count_1000_is_accepted(self) -> None:
        # Arrange / Act
        result = generate(seed=1, records=1000)

        # Assert
        assert len(result.transactions) == 1000

    @pytest.mark.parametrize("records", [999, 1, 0, -1])
    def test_records_below_1000_raise_value_error(self, records: int) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValueError):
            generate(seed=1, records=records)

    def test_same_seed_produces_equal_scenario_log(self) -> None:
        # Arrange / Act
        first = generate(seed=7, records=SMALL_RECORDS)
        second = generate(seed=7, records=SMALL_RECORDS)

        # Assert
        assert first == second

    def test_different_seed_produces_different_transactions(self, small_log: ScenarioLog) -> None:
        # Arrange / Act
        other = generate(seed=8, records=SMALL_RECORDS)

        # Assert
        assert other.transactions != small_log.transactions

    def test_write_dataset_twice_with_same_seed_is_byte_identical(self, tmp_path: Path) -> None:
        # Arrange
        first_dir = tmp_path / "first"
        second_dir = tmp_path / "second"
        first_dir.mkdir()
        second_dir.mkdir()

        # Act
        first = write_dataset(generate(seed=7, records=SMALL_RECORDS), first_dir)
        second = write_dataset(generate(seed=7, records=SMALL_RECORDS), second_dir)

        # Assert
        assert first.csv_path.read_bytes() == second.csv_path.read_bytes()
        assert first.json_path.read_bytes() == second.json_path.read_bytes()
        assert first.incidents_path.read_bytes() == second.incidents_path.read_bytes()

    def test_transactions_are_sorted_by_timestamp_ascending(self, log: ScenarioLog) -> None:
        # Arrange
        timestamps = [tx.timestamp for tx in log.transactions]

        # Act
        ordered = sorted(timestamps)

        # Assert
        assert timestamps == ordered

    def test_transaction_ids_are_unique(self, log: ScenarioLog) -> None:
        # Arrange
        ids = [tx.transaction_id for tx in log.transactions]

        # Act
        distinct = len(set(ids))

        # Assert
        assert distinct == len(ids)

    def test_all_timestamps_are_utc_and_within_october_2026(self, log: ScenarioLog) -> None:
        # Arrange
        timestamps = [tx.timestamp for tx in log.transactions]

        # Act
        out_of_range = [
            t
            for t in timestamps
            if t.tzinfo is None or t.utcoffset() != timedelta(0) or not (OCTOBER_START <= t < NOVEMBER_START)
        ]

        # Assert
        assert out_of_range == []

    def test_demo_day_is_2026_10_31(self) -> None:
        # Arrange / Act
        demo_day = DEMO_DAY

        # Assert
        assert demo_day == date(2026, 10, 31)


# --------------------------------------------------------------------------- D2: schema and enums


class TestFieldConstants:
    def test_brief_fields_has_16_unique_entries(self) -> None:
        # Arrange / Act
        distinct = len(set(BRIEF_FIELDS))

        # Assert
        assert len(BRIEF_FIELDS) == 16
        assert distinct == 16

    def test_extension_fields_match_spec(self) -> None:
        # Arrange
        expected = ("record_kind", "scenario", "card_bin", "acquirer", "rule_id", "sender_balance")

        # Act
        actual = EXTENSION_FIELDS

        # Assert
        assert actual == expected

    def test_export_fields_is_brief_followed_by_extension(self) -> None:
        # Arrange / Act
        combined = BRIEF_FIELDS + EXTENSION_FIELDS

        # Assert
        assert EXPORT_FIELDS == combined

    def test_canonical_transaction_fields_follow_export_fields_with_minor_unit_renames(self) -> None:
        # Arrange
        renames = {"amount": "amount_minor", "sender_balance": "sender_balance_minor"}
        expected = {renames.get(name, name) for name in EXPORT_FIELDS}

        # Act
        actual = {f.name for f in dataclasses.fields(CanonicalTransaction)}

        # Assert
        assert actual == expected


class TestSchemaAndEnums:
    @pytest.mark.parametrize("field", sorted(ENUMS))
    def test_enum_fields_only_contain_allowed_values(self, rows: list[Row], field: str) -> None:
        # Arrange
        allowed = ENUMS[field]

        # Act
        observed = {r[field] for r in rows}

        # Assert
        assert observed <= allowed

    @pytest.mark.parametrize("field", FULL_COVERAGE_FIELDS)
    def test_every_enum_value_appears_at_least_once(self, rows: list[Row], field: str) -> None:
        # Arrange
        expected = ENUMS[field]

        # Act
        observed = {r[field] for r in rows}

        # Assert
        assert observed == expected

    @pytest.mark.parametrize("field", ["country_sender", "country_receiver"])
    def test_countries_are_two_letter_upper_case_codes(self, rows: list[Row], field: str) -> None:
        # Arrange
        pattern = re.compile(r"^[A-Z]{2}$")

        # Act
        invalid = [r[field] for r in rows if not (isinstance(r[field], str) and pattern.match(r[field]))]

        # Assert
        assert invalid == []

    def test_fraud_score_is_within_unit_interval(self, rows: list[Row]) -> None:
        # Arrange
        scores = [float(r["fraud_score"]) for r in rows]

        # Act
        out_of_range = [s for s in scores if not (0.0 <= s <= 1.0)]

        # Assert
        assert out_of_range == []

    def test_amount_minor_is_positive_integer(self, log: ScenarioLog) -> None:
        # Arrange / Act
        invalid = [
            tx.amount_minor
            for tx in log.transactions
            if not (type(tx.amount_minor) is int and tx.amount_minor > 0)
        ]

        # Assert
        assert invalid == []

    def test_export_amount_equals_minor_units_scaled_by_currency_exponent(self, log: ScenarioLog) -> None:
        # Arrange
        mismatches: list[tuple[str, float, float]] = []

        # Act
        for tx in log.transactions:
            exponent = 0 if tx.currency == "JPY" else 2
            expected = tx.amount_minor / 10**exponent
            actual = to_export_row(tx)["amount"]
            if actual != expected:
                mismatches.append((tx.currency, expected, actual))

        # Assert
        assert mismatches == []

    def test_merchant_id_is_none_iff_record_is_p2p_transfer(self, rows: list[Row]) -> None:
        # Arrange / Act
        violations = [
            r["record_kind"]
            for r in rows
            if (r["merchant_id"] is None) != (r["record_kind"] == "p2p_transfer")
        ]

        # Assert
        assert violations == []

    def test_merchant_ids_look_like_mch_followed_by_four_digits(self, rows: list[Row]) -> None:
        # Arrange
        pattern = re.compile(r"^mch_\d{4}$")

        # Act
        invalid = [
            r["merchant_id"]
            for r in rows
            if r["merchant_id"] is not None and not pattern.match(r["merchant_id"])
        ]

        # Assert
        assert invalid == []

    def test_both_record_kinds_are_present(self, rows: list[Row]) -> None:
        # Arrange / Act
        kinds = {r["record_kind"] for r in rows}

        # Assert
        assert kinds == {"merchant_payment", "p2p_transfer"}

    def test_card_bin_is_six_digit_string_iff_payment_method_is_card(self, rows: list[Row]) -> None:
        # Arrange
        pattern = re.compile(r"^\d{6}$")

        # Act
        violations = [
            (r["payment_method"], r["card_bin"])
            for r in rows
            if (r["payment_method"] == "card")
            != (isinstance(r["card_bin"], str) and bool(pattern.match(r["card_bin"])))
            or (r["payment_method"] != "card" and r["card_bin"] is not None)
        ]

        # Assert
        assert violations == []

    def test_acquirer_is_acq_a_or_acq_b_for_card_and_none_otherwise(self, rows: list[Row]) -> None:
        # Arrange / Act
        card_acquirers = {r["acquirer"] for r in rows if r["payment_method"] == "card"}
        non_card_acquirers = {r["acquirer"] for r in rows if r["payment_method"] != "card"}

        # Assert
        assert card_acquirers <= {"acq_a", "acq_b"}
        assert non_card_acquirers == {None}

    def test_failure_reason_is_non_empty_iff_failed_or_flagged(self, rows: list[Row]) -> None:
        # Arrange / Act
        violations = [
            (r["status"], r["failure_reason"])
            for r in rows
            if (
                not _is_nonempty_str(r["failure_reason"])
                if r["status"] in {"failed", "flagged"}
                else r["failure_reason"] is not None
            )
        ]

        # Assert
        assert violations == []

    def test_resolution_notes_non_empty_for_failed_flagged_reversed_or_chargeback(
        self, rows: list[Row]
    ) -> None:
        # Arrange / Act
        violations = [
            (r["status"], r["chargeback_flag"], r["resolution_notes"])
            for r in rows
            if (
                not _is_nonempty_str(r["resolution_notes"])
                if r["status"] in {"failed", "flagged", "reversed"} or r["chargeback_flag"]
                else r["resolution_notes"] is not None
            )
        ]

        # Assert
        assert violations == []

    def test_chargeback_flag_implies_successful_disputed_or_reversed_card_payment(
        self, rows: list[Row]
    ) -> None:
        # Arrange
        chargebacks = [r for r in rows if r["chargeback_flag"] is True]

        # Act
        violations = [
            r["transaction_id"]
            for r in chargebacks
            if not (
                r["payment_method"] == "card"
                and r["status"] == "success"
                and r["settlement_status"] in {"disputed", "reversed"}
            )
        ]

        # Assert
        assert chargebacks != []
        assert violations == []

    def test_reversed_status_implies_reversed_settlement(self, rows: list[Row]) -> None:
        # Arrange
        reversed_rows = [r for r in rows if r["status"] == "reversed"]

        # Act
        settlements = {r["settlement_status"] for r in reversed_rows}

        # Assert
        assert reversed_rows != []
        assert settlements == {"reversed"}

    @pytest.mark.parametrize("status", ["failed", "flagged", "pending"])
    def test_unsettled_statuses_imply_pending_settlement(self, rows: list[Row], status: str) -> None:
        # Arrange
        matching = [r for r in rows if r["status"] == status]

        # Act
        settlements = {r["settlement_status"] for r in matching}

        # Assert
        assert matching != []
        assert settlements == {"pending"}

    def test_cross_border_records_are_at_least_five_percent(self, rows: list[Row]) -> None:
        # Arrange
        cross_border = sum(1 for r in rows if r["country_sender"] != r["country_receiver"])

        # Act
        share = cross_border / len(rows)

        # Assert
        assert share >= 0.05


# --------------------------------------------------------------------------- Export


class TestToExportRow:
    def test_keys_follow_export_fields_order(self, log: ScenarioLog) -> None:
        # Arrange / Act
        row = to_export_row(log.transactions[0])

        # Assert
        assert list(row.keys()) == list(EXPORT_FIELDS)

    def test_uuids_are_exported_as_strings_matching_canonical_values(self, log: ScenarioLog) -> None:
        # Arrange
        tx = log.transactions[0]

        # Act
        row = to_export_row(tx)

        # Assert
        assert row["transaction_id"] == str(tx.transaction_id)
        assert row["user_id"] == str(tx.user_id)

    def test_usd_amounts_use_two_decimal_exponent_and_timestamp_uses_z_suffix(self, log: ScenarioLog) -> None:
        # Arrange
        tx = dataclasses.replace(
            log.transactions[0],
            currency="USD",
            amount_minor=1234,
            sender_balance_minor=5000,
            timestamp=datetime(2026, 10, 31, 13, 5, tzinfo=UTC),
            chargeback_flag=True,
        )

        # Act
        row = to_export_row(tx)

        # Assert
        assert row["timestamp"] == "2026-10-31T13:05:00Z"
        assert row["amount"] == 12.34
        assert type(row["amount"]) is float
        assert row["sender_balance"] == 50.0
        assert type(row["sender_balance"]) is float
        assert row["chargeback_flag"] is True

    def test_jpy_amounts_use_zero_decimal_exponent(self, log: ScenarioLog) -> None:
        # Arrange
        tx = dataclasses.replace(
            log.transactions[0], currency="JPY", amount_minor=1500, sender_balance_minor=20000
        )

        # Act
        row = to_export_row(tx)

        # Assert
        assert row["amount"] == 1500.0
        assert type(row["amount"]) is float
        assert row["sender_balance"] == 20000.0

    def test_missing_sender_balance_is_exported_as_none(self, log: ScenarioLog) -> None:
        # Arrange
        tx = dataclasses.replace(log.transactions[0], sender_balance_minor=None)

        # Act
        row = to_export_row(tx)

        # Assert
        assert row["sender_balance"] is None

    def test_timestamps_in_generated_rows_are_iso_8601_with_z(self, rows: list[Row]) -> None:
        # Arrange / Act
        invalid = [
            r["timestamp"]
            for r in rows
            if not (isinstance(r["timestamp"], str) and ISO_Z.match(r["timestamp"]))
        ]

        # Assert
        assert invalid == []

    def test_non_card_rows_export_card_bin_and_acquirer_as_none(self, rows: list[Row]) -> None:
        # Arrange
        non_card = [r for r in rows if r["payment_method"] != "card"]

        # Act
        values = {(r["card_bin"], r["acquirer"]) for r in non_card}

        # Assert
        assert non_card != []
        assert values == {(None, None)}

    def test_chargeback_flag_is_exported_as_bool(self, rows: list[Row]) -> None:
        # Arrange / Act
        types = {type(r["chargeback_flag"]) for r in rows}

        # Assert
        assert types == {bool}


class TestWriteDataset:
    def test_returns_paths_with_spec_file_names_inside_out_dir(self, written: DatasetFiles) -> None:
        # Arrange / Act
        names = (written.csv_path.name, written.json_path.name, written.incidents_path.name)

        # Assert
        assert names == ("transactions.csv", "transactions.json", "incidents.json")
        assert written.csv_path.parent == written.json_path.parent == written.incidents_path.parent

    def test_all_three_files_exist(self, written: DatasetFiles) -> None:
        # Arrange / Act
        existence = [
            written.csv_path.is_file(),
            written.json_path.is_file(),
            written.incidents_path.is_file(),
        ]

        # Assert
        assert existence == [True, True, True]

    def test_csv_header_equals_export_fields(self, written: DatasetFiles) -> None:
        # Arrange / Act
        header, _ = _read_csv(written.csv_path)

        # Assert
        assert header == list(EXPORT_FIELDS)

    def test_json_is_list_of_objects_with_export_field_keys_in_order(self, written: DatasetFiles) -> None:
        # Arrange / Act
        data = json.loads(written.json_path.read_text(encoding="utf-8"))

        # Assert
        assert isinstance(data, list)
        assert {tuple(item.keys()) for item in data} == {tuple(EXPORT_FIELDS)}

    def test_json_rows_equal_to_export_row_of_each_transaction(
        self, written: DatasetFiles, small_log: ScenarioLog
    ) -> None:
        # Arrange
        expected = [to_export_row(tx) for tx in small_log.transactions]

        # Act
        data = json.loads(written.json_path.read_text(encoding="utf-8"))

        # Assert
        assert data == expected

    def test_csv_and_json_contain_the_same_number_of_rows(self, written: DatasetFiles) -> None:
        # Arrange / Act
        _, csv_rows = _read_csv(written.csv_path)
        data = json.loads(written.json_path.read_text(encoding="utf-8"))

        # Assert
        assert len(csv_rows) == SMALL_RECORDS
        assert len(data) == SMALL_RECORDS

    def test_csv_rows_coerced_back_equal_json_rows(self, written: DatasetFiles) -> None:
        # Arrange
        _, csv_rows = _read_csv(written.csv_path)
        data = json.loads(written.json_path.read_text(encoding="utf-8"))

        # Act
        coerced = [_coerce_csv_row(c, j) for c, j in zip(csv_rows, data, strict=True)]

        # Assert
        assert coerced == data

    def test_csv_encodes_none_as_empty_string(self, written: DatasetFiles) -> None:
        # Arrange
        _, csv_rows = _read_csv(written.csv_path)
        data = json.loads(written.json_path.read_text(encoding="utf-8"))

        # Act
        none_cells = [
            (field, c[field])
            for c, j in zip(csv_rows, data, strict=True)
            for field in EXPORT_FIELDS
            if j[field] is None
        ]

        # Assert
        assert none_cells != []
        assert {cell for _, cell in none_cells} == {""}

    def test_csv_encodes_booleans_as_lowercase_true_false(self, written: DatasetFiles) -> None:
        # Arrange / Act
        _, csv_rows = _read_csv(written.csv_path)
        observed = {c["chargeback_flag"] for c in csv_rows}

        # Assert
        assert observed <= {"true", "false"}
        assert "false" in observed

    def test_incidents_json_has_every_incident_field(self, written: DatasetFiles) -> None:
        # Arrange
        expected_keys = {f.name for f in dataclasses.fields(Incident)}

        # Act
        data = json.loads(written.incidents_path.read_text(encoding="utf-8"))

        # Assert
        assert isinstance(data, list)
        assert data != []
        assert [set(item.keys()) for item in data] == [expected_keys] * len(data)

    def test_incidents_json_matches_log_incidents_with_z_suffixed_datetimes(
        self, written: DatasetFiles, small_log: ScenarioLog
    ) -> None:
        # Arrange
        data = json.loads(written.incidents_path.read_text(encoding="utf-8"))

        # Act
        shape = [
            (
                item["incident_id"],
                item["merchant_id"],
                datetime.fromisoformat(item["started_at"]),
                None if item["resolved_at"] is None else datetime.fromisoformat(item["resolved_at"]),
                item["recurrence_within_7d"],
            )
            for item in data
        ]
        expected = [
            (i.incident_id, i.merchant_id, i.started_at, i.resolved_at, i.recurrence_within_7d)
            for i in small_log.incidents
        ]
        malformed = [
            item[key]
            for item in data
            for key in ("started_at", "resolved_at")
            if item[key] is not None and not ISO_Z.match(item[key])
        ]

        # Assert
        assert shape == expected
        assert malformed == []

    def test_no_exported_value_contains_a_pan_like_digit_run(self, rows: list[Row]) -> None:
        # Arrange / Act
        offenders = [
            (field, value)
            for r in rows
            for field, value in r.items()
            if value is not None and PAN_LIKE.search(str(value))
        ]

        # Assert
        assert offenders == []

    def test_no_csv_cell_contains_a_pan_like_digit_run(self, written: DatasetFiles) -> None:
        # Arrange
        _, csv_rows = _read_csv(written.csv_path)

        # Act
        offenders = [cell for r in csv_rows for cell in r.values() if PAN_LIKE.search(cell)]

        # Assert
        assert offenders == []


# --------------------------------------------------------------------------- D3: planted scenarios


class TestPlantedScenarioShape:
    def test_planted_merchants_are_distinct(self, log: ScenarioLog) -> None:
        # Arrange
        planted = log.planted
        merchants = [planted.sq1_merchant_id, planted.sq3_merchant_id, planted.sq4_merchant_id]

        # Act
        distinct = set(merchants)

        # Assert
        assert len(distinct) == 3

    def test_planted_merchants_exist_in_the_dataset(self, log: ScenarioLog, rows: list[Row]) -> None:
        # Arrange
        planted = log.planted
        expected = {planted.sq1_merchant_id, planted.sq3_merchant_id, planted.sq4_merchant_id}

        # Act
        present = {r["merchant_id"] for r in rows} & expected

        # Assert
        assert present == expected

    def test_sq1_window_is_12_to_18_utc_on_demo_day(self, log: ScenarioLog) -> None:
        # Arrange
        planted = log.planted

        # Act
        window = (planted.sq1_window_start, planted.sq1_window_end)

        # Assert
        assert window == (
            datetime(2026, 10, 31, 12, 0, tzinfo=UTC),
            datetime(2026, 10, 31, 18, 0, tzinfo=UTC),
        )


class TestSq1CardFailureSpike:
    def _window_rows(self, log: ScenarioLog, rows: list[Row]) -> list[Row]:
        p = log.planted
        return [
            r
            for r in rows
            if r["merchant_id"] == p.sq1_merchant_id
            and r["payment_method"] == "card"
            and p.sq1_window_start <= _ts(r) < p.sq1_window_end
        ]

    def test_window_has_at_least_200_card_records(self, log: ScenarioLog, rows: list[Row]) -> None:
        # Arrange / Act
        window = self._window_rows(log, rows)

        # Assert
        assert len(window) >= 200

    def test_window_failure_rate_is_between_12_and_18_percent(
        self, log: ScenarioLog, rows: list[Row]
    ) -> None:
        # Arrange
        window = self._window_rows(log, rows)

        # Act
        rate = _failure_rate(window)

        # Assert
        assert 0.12 <= rate <= 0.18

    def test_at_least_80_percent_of_window_failures_share_the_planted_bin(
        self, log: ScenarioLog, rows: list[Row]
    ) -> None:
        # Arrange
        failures = [r for r in self._window_rows(log, rows) if r["status"] == "failed"]

        # Act
        on_bin = sum(1 for r in failures if r["card_bin"] == log.planted.sq1_card_bin)

        # Assert
        assert failures != []
        assert on_bin / len(failures) >= 0.80

    def test_baseline_failure_rate_in_the_seven_days_before_demo_day_is_below_5_percent(
        self, log: ScenarioLog, rows: list[Row]
    ) -> None:
        # Arrange
        demo_start = datetime(2026, 10, 31, tzinfo=UTC)
        baseline_start = demo_start - timedelta(days=7)
        baseline = [
            r
            for r in rows
            if r["merchant_id"] == log.planted.sq1_merchant_id
            and r["payment_method"] == "card"
            and baseline_start <= _ts(r) < demo_start
        ]

        # Act
        rate = _failure_rate(baseline)

        # Assert
        assert baseline != []
        assert rate < 0.05

    def test_resolved_historical_incident_with_fix_exists_for_merchant_or_bin(self, log: ScenarioLog) -> None:
        # Arrange
        p = log.planted

        # Act
        matching = [
            i
            for i in log.incidents
            if (
                i.merchant_id == p.sq1_merchant_id
                or p.sq1_card_bin in " ".join([i.title, i.root_cause, i.fix_applied, i.resolution_notes])
            )
            and i.started_at < p.sq1_window_start
            and _is_nonempty_str(i.fix_applied)
            and i.resolved_at is not None
        ]

        # Assert
        assert matching != []


class TestSq2CrossBorderLimit:
    def test_planted_transaction_is_failed_cross_border_bank_transfer_p2p(
        self, log: ScenarioLog, rows_by_id: dict[str, Row]
    ) -> None:
        # Arrange
        row = rows_by_id[str(log.planted.sq2_transaction_id)]

        # Act
        observed = (row["record_kind"], row["payment_method"], row["status"])

        # Assert
        assert observed == ("p2p_transfer", "bank_transfer", "failed")
        assert row["country_sender"] != row["country_receiver"]

    def test_sender_had_sufficient_funds_for_the_amount(
        self, log: ScenarioLog, rows_by_id: dict[str, Row]
    ) -> None:
        # Arrange
        row = rows_by_id[str(log.planted.sq2_transaction_id)]

        # Act
        balance = row["sender_balance"]

        # Assert
        assert type(balance) is float
        assert balance >= row["amount"]

    def test_sufficient_funds_also_holds_in_canonical_minor_units(self, log: ScenarioLog) -> None:
        # Arrange
        tx = next(t for t in log.transactions if t.transaction_id == log.planted.sq2_transaction_id)

        # Act
        balance = tx.sender_balance_minor

        # Assert
        assert type(balance) is int
        assert balance >= tx.amount_minor

    def test_rule_id_is_non_empty_and_failure_reason_mentions_limit(
        self, log: ScenarioLog, rows_by_id: dict[str, Row]
    ) -> None:
        # Arrange
        row = rows_by_id[str(log.planted.sq2_transaction_id)]

        # Act
        reason = row["failure_reason"]

        # Assert
        assert _is_nonempty_str(row["rule_id"])
        assert isinstance(reason, str)
        assert "limit" in reason.lower()


class TestSq3CardTesting:
    def _series(self, log: ScenarioLog, rows_by_id: dict[str, Row]) -> list[Row]:
        return [rows_by_id[str(tid)] for tid in log.planted.sq3_transaction_ids]

    def test_series_has_at_least_30_distinct_records(self, log: ScenarioLog) -> None:
        # Arrange
        ids = log.planted.sq3_transaction_ids

        # Act
        distinct = len(set(ids))

        # Assert
        assert distinct == len(ids)
        assert distinct >= 30

    def test_series_records_are_flagged_card_micro_payments_for_the_planted_merchant(
        self, log: ScenarioLog, rows_by_id: dict[str, Row]
    ) -> None:
        # Arrange
        series = self._series(log, rows_by_id)

        # Act
        profile = {(r["merchant_id"], r["payment_method"], r["status"]) for r in series}

        # Assert
        assert profile == {(log.planted.sq3_merchant_id, "card", "flagged")}
        assert max(r["amount"] for r in series) <= 1.00

    def test_series_fraud_scores_are_at_least_0_8(self, log: ScenarioLog, rows_by_id: dict[str, Row]) -> None:
        # Arrange
        series = self._series(log, rows_by_id)

        # Act
        lowest = min(float(r["fraud_score"]) for r in series)

        # Assert
        assert lowest >= 0.8

    def test_series_falls_within_a_ten_minute_span(
        self, log: ScenarioLog, rows_by_id: dict[str, Row]
    ) -> None:
        # Arrange
        stamps = [_ts(r) for r in self._series(log, rows_by_id)]

        # Act
        span = max(stamps) - min(stamps)

        # Assert
        assert span <= timedelta(minutes=10)

    def test_series_involves_at_least_three_distinct_users(
        self, log: ScenarioLog, rows_by_id: dict[str, Row]
    ) -> None:
        # Arrange
        series = self._series(log, rows_by_id)

        # Act
        users = {r["user_id"] for r in series}

        # Assert
        assert len(users) >= 3

    def test_fewer_than_five_flagged_micro_payments_exist_outside_the_series(
        self, log: ScenarioLog, rows: list[Row]
    ) -> None:
        # Arrange
        series_ids = {str(tid) for tid in log.planted.sq3_transaction_ids}

        # Act
        outside = [
            r
            for r in rows
            if r["merchant_id"] == log.planted.sq3_merchant_id
            and r["status"] == "flagged"
            and r["amount"] <= 1.00
            and r["transaction_id"] not in series_ids
        ]

        # Assert
        assert len(outside) < 5


class TestSq4SettlementGap:
    def test_planted_merchant_has_a_chargeback(self, log: ScenarioLog, rows: list[Row]) -> None:
        # Arrange
        merchant_rows = [r for r in rows if r["merchant_id"] == log.planted.sq4_merchant_id]

        # Act
        chargebacks = [r for r in merchant_rows if r["chargeback_flag"] is True]

        # Assert
        assert len(chargebacks) >= 1

    def test_planted_merchant_has_a_refund_as_reversed_status(
        self, log: ScenarioLog, rows: list[Row]
    ) -> None:
        # Arrange
        merchant_rows = [r for r in rows if r["merchant_id"] == log.planted.sq4_merchant_id]

        # Act
        refunds = [r for r in merchant_rows if r["status"] == "reversed"]

        # Assert
        assert len(refunds) >= 1

    def test_planted_merchant_has_a_non_usd_fx_record(self, log: ScenarioLog, rows: list[Row]) -> None:
        # Arrange
        merchant_rows = [r for r in rows if r["merchant_id"] == log.planted.sq4_merchant_id]

        # Act
        currencies = Counter(r["currency"] for r in merchant_rows)
        non_usd = sum(count for currency, count in currencies.items() if currency != "USD")

        # Assert
        assert non_usd >= 1


# --------------------------------------------------------------------------- CLI


class TestCli:
    def test_main_returns_zero_and_writes_three_files_with_requested_row_count(self, tmp_path: Path) -> None:
        # Arrange
        argv = ["--seed", "7", "--records", str(SMALL_RECORDS), "--out", str(tmp_path)]

        # Act
        exit_code = main(argv)

        # Assert
        assert exit_code == 0
        assert (tmp_path / "transactions.csv").is_file()
        assert (tmp_path / "incidents.json").is_file()
        data = json.loads((tmp_path / "transactions.json").read_text(encoding="utf-8"))
        _, csv_rows = _read_csv(tmp_path / "transactions.csv")
        assert len(data) == SMALL_RECORDS
        assert len(csv_rows) == SMALL_RECORDS

    def test_main_output_is_identical_to_write_dataset_with_same_seed_and_records(
        self, tmp_path: Path
    ) -> None:
        # Arrange
        cli_dir = tmp_path / "cli"
        api_dir = tmp_path / "api"
        cli_dir.mkdir()
        api_dir.mkdir()

        # Act
        main(["--seed", "7", "--records", str(SMALL_RECORDS), "--out", str(cli_dir)])
        expected = write_dataset(generate(seed=7, records=SMALL_RECORDS), api_dir)

        # Assert
        assert (cli_dir / "transactions.csv").read_bytes() == expected.csv_path.read_bytes()
        assert (cli_dir / "transactions.json").read_bytes() == expected.json_path.read_bytes()
        assert (cli_dir / "incidents.json").read_bytes() == expected.incidents_path.read_bytes()
