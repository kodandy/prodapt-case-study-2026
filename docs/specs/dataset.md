# Synthetic dataset — test specification (Day 1, D1–D3)

Source: the brief's dataset section (16 fields), `docs/architecture-detail.md` §2, `docs/requirements-traceability.md` (SQ1–SQ4, D1–D3).
Module: `scripts/generate_dataset.py` (stdlib only, no Faker). Tests: `scripts/__tests__/test_generate_dataset.py`. Pure functions: no DB, no network.

## Public interface

```python
DEMO_DAY: date                      # 2026-10-31 (UTC); "this afternoon" = 12:00–18:00 UTC on this day
BRIEF_FIELDS: tuple[str, ...]       # the 16 brief fields, in brief order
EXTENSION_FIELDS: tuple[str, ...]   # ("record_kind", "scenario", "card_bin", "acquirer", "rule_id", "sender_balance")
EXPORT_FIELDS = BRIEF_FIELDS + EXTENSION_FIELDS

@dataclass(frozen=True) class CanonicalTransaction   # one field per EXPORT_FIELDS name, except:
    #   amount_minor: int (not amount), sender_balance_minor: int | None (not sender_balance),
    #   timestamp: datetime (tz-aware UTC), transaction_id / user_id: uuid.UUID
@dataclass(frozen=True) class Incident(incident_id: str, title: str, merchant_id: str | None, started_at: datetime,
    resolved_at: datetime | None, root_cause: str, fix_applied: str, recurrence_within_7d: bool, resolution_notes: str)
@dataclass(frozen=True) class PlantedScenarios(
    sq1_merchant_id: str, sq1_window_start: datetime, sq1_window_end: datetime, sq1_card_bin: str,
    sq2_transaction_id: uuid.UUID,
    sq3_merchant_id: str, sq3_transaction_ids: tuple[uuid.UUID, ...],
    sq4_merchant_id: str)
@dataclass(frozen=True) class ScenarioLog(transactions: tuple[CanonicalTransaction, ...], incidents: tuple[Incident, ...],
    planted: PlantedScenarios)
@dataclass(frozen=True) class DatasetFiles(csv_path: Path, json_path: Path, incidents_path: Path)

def generate(seed: int = 42, records: int = 12_000) -> ScenarioLog
def to_export_row(tx: CanonicalTransaction) -> dict[str, str | float | int | bool | None]
def write_dataset(log: ScenarioLog, out_dir: Path) -> DatasetFiles   # transactions.csv, transactions.json, incidents.json
def main(argv: list[str] | None = None) -> int                       # --seed, --records, --out; returns 0
```

`generate` raises `ValueError` if `records < 1000` (planted scenarios need room).

## Behavior

### Reproducibility (D1)
- `len(log.transactions) == records`; default `records` is 12,000 (≥ 10,000).
- Same seed → equal `ScenarioLog`; `write_dataset` twice with the same seed → byte-identical files. A different seed → different transactions.
- Transactions are sorted by `timestamp` ascending; `transaction_id` values are unique.
- All timestamps fall in [2026-10-01T00:00Z, 2026-11-01T00:00Z).

### Schema and enums (D2)
- `payment_method` ∈ {card, bank_transfer, wallet, upi}; `status` ∈ {success, failed, pending, flagged, reversed};
  `settlement_status` ∈ {settled, pending, disputed, reversed}; `kyc_status` ∈ {verified, pending, failed};
  `currency` ∈ {USD, EUR, INR, JPY}; countries are 2-letter upper-case codes; `fraud_score` ∈ [0.0, 1.0];
  `record_kind` ∈ {merchant_payment, p2p_transfer}.
- `amount_minor > 0`. Export `amount` = `amount_minor / 10**exponent` (JPY exponent 0, others 2) as float.
- `merchant_id is None` iff `record_kind == "p2p_transfer"`. Merchant ids look like `mch_0001`.
- `card_bin` is a 6-digit string iff `payment_method == "card"`, else None. `acquirer` ∈ {acq_a, acq_b} for card, else None.
- `failure_reason` is a non-empty string iff `status in {"failed", "flagged"}`, else None.
- `resolution_notes` is a non-empty string when `status in {"failed", "flagged", "reversed"}` or `chargeback_flag`; else None.
- `chargeback_flag` ⇒ `payment_method == "card"`, `status == "success"`, `settlement_status in {"disputed", "reversed"}`.
- `status == "reversed"` ⇒ `settlement_status == "reversed"`. `status in {"failed", "flagged", "pending"}` ⇒ `settlement_status == "pending"`.
- At least one record of every `payment_method`, `status`, `settlement_status`, `kyc_status` value.
- Cross-border records (`country_sender != country_receiver`) are ≥ 5% of all.

### Export (CSV / JSON)
- CSV header == `EXPORT_FIELDS`. JSON is a list of objects whose keys == `EXPORT_FIELDS` in that order.
- `to_export_row` types: UUIDs as str, `timestamp` ISO 8601 with `Z` (e.g. `2026-10-31T13:05:00Z`), `amount` float, `chargeback_flag` bool, `sender_balance` float or None (same exponent rule), others str or None.
- CSV encodes None as empty string and booleans as `true`/`false`. Parsing the CSV back and coercing types yields the JSON rows (one canonical source).
- No field in any row contains a 13–19 digit run (no PAN-like values).
- `incidents.json`: list of objects with every `Incident` field; datetimes ISO 8601 `Z`, `resolved_at` may be null.

### Planted scenarios (D3) — each must be detectable from export rows alone
- **SQ1 card failure spike**: for `sq1_merchant_id`, card records with timestamp in [`sq1_window_start`, `sq1_window_end`) (12:00–18:00 UTC on DEMO_DAY): ≥ 200 records, failure rate (`status == "failed"`) between 0.12 and 0.18; ≥ 80% of those failures have `card_bin == sq1_card_bin`. Same merchant, card, the 7 days before DEMO_DAY: failure rate < 0.05. At least one historical `Incident` for that merchant or BIN outage has a non-empty `fix_applied` and `resolved_at`.
- **SQ2 cross-border limit**: the record `sq2_transaction_id` is `p2p_transfer`, `bank_transfer`, `status == "failed"`, countries differ, `sender_balance_minor >= amount_minor` (funds sufficient), `rule_id` non-empty, and `failure_reason` mentions the limit (contains "limit").
- **SQ3 card testing**: `sq3_transaction_ids` has ≥ 30 records, all `sq3_merchant_id`, card, amount ≤ 1.00 in currency units, within a 10-minute span, `status == "flagged"`, `fraud_score >= 0.8`, ≥ 3 distinct `user_id`. Outside that series, flagged micro-payments (≤ 1.00) for that merchant are < 5.
- **SQ4 settlement gap**: `sq4_merchant_id` has, in October, ≥ 1 chargeback (`chargeback_flag`), ≥ 1 `status == "reversed"` (refund), and ≥ 1 non-USD record (FX).
- Planted merchants are distinct from each other.

### CLI
- `main(["--seed", "7", "--records", "2000", "--out", str(tmp)])` returns 0 and writes the three files; rows == 2000.
