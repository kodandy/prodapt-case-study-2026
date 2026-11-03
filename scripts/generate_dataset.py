"""Seeded synthetic dataset: one canonical scenario log -> the brief's 16-field CSV/JSON export.

Usage: python scripts/generate_dataset.py --seed 42 --records 12000 --out data/generated
"""

import argparse
import csv
import io
import json
import random
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal

DEMO_DAY = date(2026, 10, 31)
PERIOD_START = datetime(2026, 10, 1, tzinfo=UTC)
PERIOD_END = datetime(2026, 11, 1, tzinfo=UTC)

BRIEF_FIELDS: tuple[str, ...] = (
    "transaction_id",
    "user_id",
    "merchant_id",
    "amount",
    "currency",
    "payment_method",
    "status",
    "failure_reason",
    "fraud_score",
    "timestamp",
    "chargeback_flag",
    "settlement_status",
    "resolution_notes",
    "kyc_status",
    "country_sender",
    "country_receiver",
)
EXTENSION_FIELDS: tuple[str, ...] = (
    "record_kind",
    "scenario",
    "card_bin",
    "acquirer",
    "rule_id",
    "sender_balance",
)
EXPORT_FIELDS = BRIEF_FIELDS + EXTENSION_FIELDS

PaymentMethod = Literal["card", "bank_transfer", "wallet", "upi"]
Status = Literal["success", "failed", "pending", "flagged", "reversed"]
SettlementStatus = Literal["settled", "pending", "disputed", "reversed"]
KycStatus = Literal["verified", "pending", "failed"]
RecordKind = Literal["merchant_payment", "p2p_transfer"]
Scenario = Literal[
    "baseline", "sq1_bin_outage", "sq2_cross_border_limit", "sq3_card_testing", "sq4_settlement"
]
ExportValue = str | float | int | bool | None

MINOR_PER_UNIT = {"USD": 100, "EUR": 100, "INR": 100, "JPY": 1}
COUNTRY_CURRENCY = {"US": "USD", "GB": "USD", "SG": "USD", "AE": "USD", "DE": "EUR", "FR": "EUR"}
COUNTRY_CURRENCY |= {"IN": "INR", "JP": "JPY"}
COUNTRIES = ("US", "GB", "DE", "FR", "IN", "JP", "SG", "AE")
COUNTRY_WEIGHTS = (30, 10, 10, 8, 20, 10, 7, 5)
UNITS_PER_USD = {"USD": 1.0, "EUR": 0.92, "INR": 84.0, "JPY": 150.0}

SQ1_MERCHANT = "mch_0001"
SQ3_MERCHANT = "mch_0002"
SQ4_MERCHANT = "mch_0003"
MERCHANT_COUNT = 40
USER_COUNT = 3000
SQ1_BIN = "400012"
CARD_BINS = ("424242", "555555", "411111", "522222", "601100", SQ1_BIN)

SQ1_WINDOW_RECORDS = 240
SQ1_WINDOW_FAILURES = 36
SQ1_WINDOW_BIN_FAILURES = 32
SQ1_BASELINE_DAYS = 7
SQ1_BASELINE_PER_DAY = 60
SQ3_SERIES = 40
SQ3_USERS = 5
SQ4_RECORDS = 60
SQ4_CHARGEBACKS = 3
SQ4_REFUNDS = 5
SQ4_EUR = 10

CARD_FAILURES = (
    "Issuer declined: do_not_honor",
    "Insufficient funds on card",
    "Card expired",
    "Incorrect CVC",
    "Issuer unavailable (timeout)",
)
TRANSFER_FAILURES = ("Beneficiary account closed", "Invalid beneficiary details", "Insufficient balance")
WALLET_FAILURES = ("Wallet balance too low", "Wallet session expired")
UPI_FAILURES = ("UPI PIN incorrect", "Remitter bank unavailable", "UPI collect request expired")
FLAG_REASONS = (
    "Flagged by fraud model: velocity anomaly",
    "Flagged by fraud model: device mismatch",
    "Flagged by fraud model: unusual geography",
)


@dataclass(frozen=True)
class CanonicalTransaction:
    transaction_id: uuid.UUID
    user_id: uuid.UUID
    merchant_id: str | None
    amount_minor: int
    currency: str
    payment_method: PaymentMethod
    status: Status
    failure_reason: str | None
    fraud_score: float
    timestamp: datetime
    chargeback_flag: bool
    settlement_status: SettlementStatus
    resolution_notes: str | None
    kyc_status: KycStatus
    country_sender: str
    country_receiver: str
    record_kind: RecordKind
    scenario: Scenario
    card_bin: str | None
    acquirer: str | None
    rule_id: str | None
    sender_balance_minor: int | None


@dataclass(frozen=True)
class Incident:
    incident_id: str
    title: str
    merchant_id: str | None
    started_at: datetime
    resolved_at: datetime | None
    root_cause: str
    fix_applied: str
    recurrence_within_7d: bool
    resolution_notes: str


@dataclass(frozen=True)
class PlantedScenarios:
    sq1_merchant_id: str
    sq1_window_start: datetime
    sq1_window_end: datetime
    sq1_card_bin: str
    sq2_transaction_id: uuid.UUID
    sq3_merchant_id: str
    sq3_transaction_ids: tuple[uuid.UUID, ...]
    sq4_merchant_id: str


@dataclass(frozen=True)
class ScenarioLog:
    transactions: tuple[CanonicalTransaction, ...]
    incidents: tuple[Incident, ...]
    planted: PlantedScenarios


@dataclass(frozen=True)
class DatasetFiles:
    csv_path: Path
    json_path: Path
    incidents_path: Path


@dataclass(frozen=True)
class _User:
    user_id: uuid.UUID
    country: str
    kyc_status: KycStatus


@dataclass(frozen=True)
class _Merchant:
    merchant_id: str
    country: str


class _Builder:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)  # noqa: S311 - reproducible synthetic data, not a secret
        self.users = tuple(
            _User(
                self.uuid(),
                self.country(),
                self.rng.choices(("verified", "pending", "failed"), (85, 10, 5))[0],
            )
            for _ in range(USER_COUNT)
        )
        self.merchants = tuple(
            _Merchant(f"mch_{index:04d}", self.country()) for index in range(1, MERCHANT_COUNT + 1)
        )

    def uuid(self) -> uuid.UUID:
        return uuid.UUID(int=self.rng.getrandbits(128), version=4)

    def country(self) -> str:
        return self.rng.choices(COUNTRIES, COUNTRY_WEIGHTS)[0]

    def merchant(self, merchant_id: str) -> _Merchant:
        return next(m for m in self.merchants if m.merchant_id == merchant_id)

    def verified_user(self) -> _User:
        return self.rng.choice([u for u in self.users if u.kyc_status == "verified"])

    def instant(self, start: datetime, end: datetime) -> datetime:
        seconds = int((end - start).total_seconds())
        return start + timedelta(seconds=self.rng.randrange(seconds))

    def amount_minor(self, currency: str, low_usd: float = 5.0, high_usd: float = 2000.0) -> int:
        usd = min(max(self.rng.lognormvariate(3.6, 1.0), low_usd), high_usd)
        units = usd * UNITS_PER_USD[currency]
        return max(1, round(units * MINOR_PER_UNIT[currency]))

    def low_fraud_score(self) -> float:
        return round(min(self.rng.betavariate(2, 12), 0.69), 4)

    def high_fraud_score(self) -> float:
        return round(self.rng.uniform(0.8, 0.99), 4)

    def card(self) -> tuple[str, str]:
        bins = [b for b in CARD_BINS if b != SQ1_BIN]
        return self.rng.choice(bins), self.rng.choice(("acq_a", "acq_b"))


def generate(seed: int = 42, records: int = 12_000) -> ScenarioLog:
    """Builds the canonical log: planted SQ1-SQ4 scenarios plus a seeded baseline filling up to `records`."""
    if records < 1000:
        raise ValueError("records must be >= 1000 to hold the planted scenarios")
    b = _Builder(seed)
    sq1 = _plant_sq1(b)
    sq2 = _plant_sq2(b)
    sq3 = _plant_sq3(b)
    sq4 = _plant_sq4(b)
    planted = [*sq1, sq2, *sq3, *sq4]
    baseline = [_baseline_record(b) for _ in range(records - len(planted))]
    transactions = sorted([*planted, *baseline], key=lambda tx: (tx.timestamp, tx.transaction_id))
    window_start = datetime.combine(DEMO_DAY, time(12), tzinfo=UTC)
    return ScenarioLog(
        transactions=tuple(transactions),
        incidents=_incidents(),
        planted=PlantedScenarios(
            sq1_merchant_id=SQ1_MERCHANT,
            sq1_window_start=window_start,
            sq1_window_end=window_start + timedelta(hours=6),
            sq1_card_bin=SQ1_BIN,
            sq2_transaction_id=sq2.transaction_id,
            sq3_merchant_id=SQ3_MERCHANT,
            sq3_transaction_ids=tuple(tx.transaction_id for tx in sq3),
            sq4_merchant_id=SQ4_MERCHANT,
        ),
    )


def _merchant_card_tx(
    b: _Builder,
    merchant_id: str,
    at: datetime,
    scenario: Scenario,
    *,
    status: Status = "success",
    failure_reason: str | None = None,
    card_bin: str | None = None,
    currency: str | None = None,
    amount_minor: int | None = None,
    chargeback: bool = False,
    user: _User | None = None,
) -> CanonicalTransaction:
    merchant = b.merchant(merchant_id)
    payer = user or b.verified_user()
    currency = currency or COUNTRY_CURRENCY[merchant.country]
    default_bin, acquirer = b.card()
    settlement: SettlementStatus = _settlement_for(b, status, chargeback, at)
    return CanonicalTransaction(
        transaction_id=b.uuid(),
        user_id=payer.user_id,
        merchant_id=merchant_id,
        amount_minor=amount_minor if amount_minor is not None else b.amount_minor(currency),
        currency=currency,
        payment_method="card",
        status=status,
        failure_reason=failure_reason,
        fraud_score=b.high_fraud_score() if status == "flagged" else b.low_fraud_score(),
        timestamp=at,
        chargeback_flag=chargeback,
        settlement_status=settlement,
        resolution_notes=_resolution_notes(b, status, chargeback, failure_reason),
        kyc_status=payer.kyc_status,
        country_sender=payer.country,
        country_receiver=merchant.country,
        record_kind="merchant_payment",
        scenario=scenario,
        card_bin=card_bin or default_bin,
        acquirer=acquirer,
        rule_id=None,
        sender_balance_minor=None,
    )


def _plant_sq1(b: _Builder) -> list[CanonicalTransaction]:
    """Card failure spike on one BIN for SQ1_MERCHANT, 12:00-18:00 UTC on DEMO_DAY; quiet week before."""
    out: list[CanonicalTransaction] = []
    for day_offset in range(SQ1_BASELINE_DAYS, 0, -1):
        day_start = datetime.combine(DEMO_DAY - timedelta(days=day_offset), time(), tzinfo=UTC)
        for index in range(SQ1_BASELINE_PER_DAY):
            failed = index == 0
            out.append(
                _merchant_card_tx(
                    b,
                    SQ1_MERCHANT,
                    b.instant(day_start, day_start + timedelta(days=1)),
                    "sq1_bin_outage",
                    status="failed" if failed else "success",
                    failure_reason="Issuer declined: do_not_honor" if failed else None,
                    card_bin=SQ1_BIN if index % 4 == 1 else None,
                )
            )
    window_start = datetime.combine(DEMO_DAY, time(12), tzinfo=UTC)
    for index in range(SQ1_WINDOW_RECORDS):
        at = b.instant(window_start, window_start + timedelta(hours=6))
        if index < SQ1_WINDOW_BIN_FAILURES:
            tx = _merchant_card_tx(
                b,
                SQ1_MERCHANT,
                at,
                "sq1_bin_outage",
                status="failed",
                failure_reason=f"Issuer unavailable (timeout) for BIN {SQ1_BIN}",
                card_bin=SQ1_BIN,
            )
        elif index < SQ1_WINDOW_FAILURES:
            tx = _merchant_card_tx(
                b, SQ1_MERCHANT, at, "sq1_bin_outage", status="failed", failure_reason="Card expired"
            )
        else:
            tx = _merchant_card_tx(
                b, SQ1_MERCHANT, at, "sq1_bin_outage", card_bin=SQ1_BIN if index % 10 == 0 else None
            )
        out.append(tx)
    return out


def _plant_sq2(b: _Builder) -> CanonicalTransaction:
    """International transfer declined by the cross-border limit for KYC tier 1, although funds suffice."""
    sender = next(u for u in b.users if u.country == "US" and u.kyc_status == "verified")
    receiver = next(u for u in b.users if u.country == "IN")
    amount_minor = 180_000
    return CanonicalTransaction(
        transaction_id=b.uuid(),
        user_id=sender.user_id,
        merchant_id=None,
        amount_minor=amount_minor,
        currency="USD",
        payment_method="bank_transfer",
        status="failed",
        failure_reason=(
            "Cross-border per-transaction limit exceeded for KYC tier 1 (1800.00 USD > 1000.00 USD)"
        ),
        fraud_score=0.12,
        timestamp=datetime.combine(DEMO_DAY, time(10, 42, 7), tzinfo=UTC),
        chargeback_flag=False,
        settlement_status="pending",
        resolution_notes=(
            "Advised sender to upgrade KYC to tier 2 or split the transfer below the tier-1 limit."
        ),
        kyc_status=sender.kyc_status,
        country_sender=sender.country,
        country_receiver=receiver.country,
        record_kind="p2p_transfer",
        scenario="sq2_cross_border_limit",
        card_bin=None,
        acquirer=None,
        rule_id="xb_limit_kyc_tier1_v3",
        sender_balance_minor=525_000,
    )


def _plant_sq3(b: _Builder) -> list[CanonicalTransaction]:
    """Card-testing burst: many sub-1.00 USD card payments in under 10 minutes from a few accounts."""
    users = [b.verified_user() for _ in range(SQ3_USERS)]
    start = datetime.combine(DEMO_DAY, time(3, 10), tzinfo=UTC)
    return [
        _merchant_card_tx(
            b,
            SQ3_MERCHANT,
            start + timedelta(seconds=index * 12 + b.rng.randrange(10)),
            "sq3_card_testing",
            status="flagged",
            failure_reason="Flagged by fraud model: card-testing pattern (micro-amount burst)",
            currency="USD",
            amount_minor=b.rng.randrange(30, 101),
            user=users[index % SQ3_USERS],
        )
        for index in range(SQ3_SERIES)
    ]


def _plant_sq4(b: _Builder) -> list[CanonicalTransaction]:
    """A merchant whose October payout is reduced by refunds, chargebacks and FX."""
    out: list[CanonicalTransaction] = []
    for index in range(SQ4_RECORDS):
        at = b.instant(PERIOD_START, datetime.combine(DEMO_DAY, time(), tzinfo=UTC))
        chargeback = index < SQ4_CHARGEBACKS
        refund = SQ4_CHARGEBACKS <= index < SQ4_CHARGEBACKS + SQ4_REFUNDS
        eur = index >= SQ4_RECORDS - SQ4_EUR
        out.append(
            _merchant_card_tx(
                b,
                SQ4_MERCHANT,
                at,
                "sq4_settlement",
                status="reversed" if refund else "success",
                chargeback=chargeback,
                currency="EUR" if eur else "USD",
            )
        )
    return out


def _baseline_record(b: _Builder) -> CanonicalTransaction:
    rng = b.rng
    payer = rng.choice(b.users)
    at = b.instant(PERIOD_START, PERIOD_END)
    p2p = rng.random() < 0.12
    method: PaymentMethod
    if p2p:
        method = rng.choice(("bank_transfer", "wallet", "upi"))
        merchant_id: str | None = None
        receiver_country = rng.choice(b.users).country
    else:
        merchant = rng.choice([m for m in b.merchants if m.merchant_id != SQ1_MERCHANT])
        merchant_id = merchant.merchant_id
        receiver_country = merchant.country
        method = rng.choices(("card", "wallet", "upi", "bank_transfer"), (60, 15, 15, 10))[0]
    if method == "upi":
        currency = "INR"
    elif p2p:
        currency = COUNTRY_CURRENCY[payer.country]
    else:
        currency = COUNTRY_CURRENCY[receiver_country]
    status: Status = rng.choices(
        ("success", "failed", "pending", "flagged", "reversed"), (900, 50, 20, 10, 20)
    )[0]
    rule_id: str | None = None
    failure_reason: str | None = None
    if p2p and payer.kyc_status == "failed" and status == "success":
        status, failure_reason, rule_id = (
            "failed",
            "Transfer blocked: sender KYC verification failed",
            "kyc_gate_v1",
        )
    elif status == "failed":
        failure_reason = rng.choice(_failure_reasons(method))
    elif status == "flagged":
        failure_reason = rng.choice(FLAG_REASONS)
    chargeback = method == "card" and status == "success" and rng.random() < 0.006
    amount_minor = b.amount_minor(currency)
    balance: int | None = None
    if p2p:
        short = failure_reason == "Insufficient balance"
        factor = rng.uniform(0.1, 0.9) if short else rng.uniform(1.05, 6.0)
        balance = max(0, round(amount_minor * factor))
    card_bin, acquirer = b.card() if method == "card" else (None, None)
    return CanonicalTransaction(
        transaction_id=b.uuid(),
        user_id=payer.user_id,
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        payment_method=method,
        status=status,
        failure_reason=failure_reason,
        fraud_score=b.high_fraud_score() if status == "flagged" else b.low_fraud_score(),
        timestamp=at,
        chargeback_flag=chargeback,
        settlement_status=_settlement_for(b, status, chargeback, at),
        resolution_notes=_resolution_notes(b, status, chargeback, failure_reason),
        kyc_status=payer.kyc_status,
        country_sender=payer.country,
        country_receiver=receiver_country,
        record_kind="p2p_transfer" if p2p else "merchant_payment",
        scenario="baseline",
        card_bin=card_bin,
        acquirer=acquirer,
        rule_id=rule_id,
        sender_balance_minor=balance,
    )


def _failure_reasons(method: PaymentMethod) -> tuple[str, ...]:
    return {
        "card": CARD_FAILURES,
        "bank_transfer": TRANSFER_FAILURES,
        "wallet": WALLET_FAILURES,
        "upi": UPI_FAILURES,
    }[method]


def _settlement_for(b: _Builder, status: Status, chargeback: bool, at: datetime) -> SettlementStatus:
    if chargeback:
        return "disputed" if b.rng.random() < 0.7 else "reversed"
    if status == "reversed":
        return "reversed"
    if status != "success":
        return "pending"
    recent = at >= PERIOD_END - timedelta(days=2)
    return "pending" if recent and b.rng.random() < 0.5 else "settled"


def _resolution_notes(
    b: _Builder, status: Status, chargeback: bool, failure_reason: str | None
) -> str | None:
    if chargeback:
        return b.rng.choice(
            (
                "Chargeback raised by cardholder (item not received); evidence requested from merchant.",
                "Chargeback raised (fraudulent transaction claim); funds held pending representment.",
            )
        )
    if status == "reversed":
        return "Refunded to the original payment method at the merchant's request."
    if status == "flagged":
        return "Held for manual fraud review; customer contacted for verification."
    if status == "failed":
        return f"Customer notified of failure ({failure_reason}); retry advised with another method."
    return None


def _incidents() -> tuple[Incident, ...]:
    def at(month: int, day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, month, day, hour, minute, tzinfo=UTC)

    return (
        Incident(
            "inc_0001",
            f"Card failures concentrated on BIN {SQ1_BIN} (issuer timeouts)",
            SQ1_MERCHANT,
            at(9, 14, 13),
            at(9, 14, 16, 40),
            f"Issuer behind BIN {SQ1_BIN} returned timeouts through acquirer acq_a.",
            f"Rerouted BIN {SQ1_BIN} to acquirer acq_b until the issuer recovered.",
            False,
            "Failure rate returned to baseline within 20 minutes of the reroute; reroute reverted next day.",
        ),
        Incident(
            "inc_0002",
            "UPI collect requests expiring",
            None,
            at(9, 22, 8),
            at(9, 22, 9, 15),
            "Remitter bank PSP latency above the collect timeout.",
            "Extended the collect timeout from 5 to 8 minutes.",
            True,
            "Recurred on 2026-09-27; PSP has since added capacity.",
        ),
        Incident(
            "inc_0003",
            "Card-testing burst on a low-ticket merchant",
            SQ3_MERCHANT,
            at(10, 3, 2, 5),
            at(10, 3, 3, 0),
            "Bot submitting sub-1.00 USD authorizations with enumerated cards.",
            "Enabled velocity rule (>= 10 micro-amount attempts per 10 minutes per merchant) and CAPTCHA.",
            False,
            "No further bursts for 14 days; rule kept enabled.",
        ),
        Incident(
            "inc_0004",
            "Settlement payout lower than expected",
            SQ4_MERCHANT,
            at(10, 10, 9),
            at(10, 10, 11),
            "Chargeback fees and rolling reserve were not shown in the merchant's own calculation.",
            "Sent itemized settlement breakdown and added reserve line to the statement.",
            False,
            "Merchant confirmed the breakdown matched their records.",
        ),
        Incident(
            "inc_0005",
            "Acquirer acq_b elevated soft declines",
            None,
            at(10, 18, 14),
            None,
            "Under investigation with the acquirer.",
            "Traffic weight on acq_b reduced from 50% to 30%.",
            False,
            "Monitoring; acquirer ticket open.",
        ),
    )


def to_export_row(tx: CanonicalTransaction) -> dict[str, ExportValue]:
    scale = MINOR_PER_UNIT[tx.currency]
    balance = None if tx.sender_balance_minor is None else tx.sender_balance_minor / scale
    row: dict[str, ExportValue] = {
        "transaction_id": str(tx.transaction_id),
        "user_id": str(tx.user_id),
        "merchant_id": tx.merchant_id,
        "amount": tx.amount_minor / scale,
        "currency": tx.currency,
        "payment_method": tx.payment_method,
        "status": tx.status,
        "failure_reason": tx.failure_reason,
        "fraud_score": tx.fraud_score,
        "timestamp": _iso(tx.timestamp),
        "chargeback_flag": tx.chargeback_flag,
        "settlement_status": tx.settlement_status,
        "resolution_notes": tx.resolution_notes,
        "kyc_status": tx.kyc_status,
        "country_sender": tx.country_sender,
        "country_receiver": tx.country_receiver,
        "record_kind": tx.record_kind,
        "scenario": tx.scenario,
        "card_bin": tx.card_bin,
        "acquirer": tx.acquirer,
        "rule_id": tx.rule_id,
        "sender_balance": balance,
    }
    return {name: row[name] for name in EXPORT_FIELDS}


def write_dataset(log: ScenarioLog, out_dir: Path) -> DatasetFiles:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [to_export_row(tx) for tx in log.transactions]
    files = DatasetFiles(
        csv_path=out_dir / "transactions.csv",
        json_path=out_dir / "transactions.json",
        incidents_path=out_dir / "incidents.json",
    )
    files.csv_path.write_text(_to_csv(rows), encoding="utf-8")
    files.json_path.write_text(json.dumps(rows, indent=1) + "\n", encoding="utf-8")
    incidents = [_incident_row(incident) for incident in log.incidents]
    files.incidents_path.write_text(json.dumps(incidents, indent=1) + "\n", encoding="utf-8")
    return files


def _to_csv(rows: list[dict[str, ExportValue]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(EXPORT_FIELDS)
    for row in rows:
        writer.writerow([_csv_cell(row[name]) for name in EXPORT_FIELDS])
    return buffer.getvalue()


def _csv_cell(value: ExportValue) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _incident_row(incident: Incident) -> dict[str, str | bool | None]:
    row: dict[str, str | bool | None] = {}
    for name, value in asdict(incident).items():
        row[name] = _iso(value) if isinstance(value, datetime) else value
    return row


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--records", type=int, default=12_000)
    parser.add_argument("--out", type=Path, default=Path("data/generated"))
    args = parser.parse_args(argv)
    log = generate(seed=args.seed, records=args.records)
    files = write_dataset(log, args.out)
    print(f"wrote {len(log.transactions)} transactions to {files.csv_path} and {files.json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BRIEF_FIELDS",
    "DEMO_DAY",
    "EXPORT_FIELDS",
    "EXTENSION_FIELDS",
    "CanonicalTransaction",
    "DatasetFiles",
    "Incident",
    "PlantedScenarios",
    "ScenarioLog",
    "generate",
    "main",
    "to_export_row",
    "write_dataset",
]
