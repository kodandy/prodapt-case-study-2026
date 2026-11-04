import argparse
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock, system_clock
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.seed import merchant_uuid, seed_shift
from ops_ai.settlement.models import SettlementEntry
from ops_ai.tickets.sources import open_dispute_ticket
from scripts.generate_dataset import ScenarioLog, generate

INGEST_SOURCE = "seed"
BATCH_SIZE = 500
CAPTURED_STATUSES = ("success", "reversed")


def seed_settlement(
    session_factory: sessionmaker[Session], log: ScenarioLog, anchor: datetime, clock: Clock = system_clock
) -> int:
    """Loads one settlement entry per captured merchant card payment at or before the anchor (same shift as
    the payment seed). The period is the month of the unshifted time: the label on the acquirer's report.
    Each newly inserted charged-back entry opens a dispute ticket in the same transaction (B12.1).
    Idempotent: a re-run inserts nothing and returns 0.
    """
    shift = seed_shift(log.planted, anchor)
    rows: list[dict[str, object]] = []
    for tx in log.transactions:
        captured_at = tx.timestamp + shift
        if (
            tx.record_kind != "merchant_payment"
            or tx.payment_method != "card"
            or tx.merchant_id is None
            or tx.status not in CAPTURED_STATUSES
            or captured_at > anchor
        ):
            continue
        rows.append(
            {
                "payment_id": tx.transaction_id,
                "merchant_id": merchant_uuid(tx.merchant_id),
                "amount_minor": tx.amount_minor,
                "currency": tx.currency,
                "captured_at": captured_at,
                "statement_period": tx.timestamp.strftime("%Y-%m"),
                "refunded": tx.status == "reversed",
                "chargeback": tx.chargeback_flag,
                "ingest_source": INGEST_SOURCE,
            }
        )
    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        statement = (
            insert(SettlementEntry)
            .values(rows[start : start + BATCH_SIZE])
            .on_conflict_do_nothing(index_elements=[SettlementEntry.payment_id])
            .returning(
                SettlementEntry.payment_id,
                SettlementEntry.merchant_id,
                SettlementEntry.amount_minor,
                SettlementEntry.currency,
                SettlementEntry.chargeback,
            )
        )
        with session_factory.begin() as session:
            new_rows = session.execute(statement).all()
            inserted += len(new_rows)
            for row in new_rows:
                if row.chargeback:
                    open_dispute_ticket(
                        session,
                        clock,
                        payment_id=row.payment_id,
                        merchant_id=row.merchant_id,
                        amount_minor=row.amount_minor,
                        currency=row.currency,
                    )
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    """python -m ops_ai.settlement: prints the inserted count only, never merchant or payment ids."""
    parser = argparse.ArgumentParser(prog="python -m ops_ai.settlement")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--records", type=int, default=12000)
    parser.add_argument(
        "--anchor", type=_aware_datetime, default=None, help="ISO 8601 with offset; default now"
    )
    args = parser.parse_args(argv)
    anchor: datetime = args.anchor or system_clock()
    engine = make_engine(load_ops_ai_settings().database_url)
    try:
        inserted = seed_settlement(
            make_session_factory(engine), generate(seed=args.seed, records=args.records), anchor
        )
    finally:
        engine.dispose()
    print(f"settlement entries: {inserted} inserted")
    return 0


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("anchor needs a UTC offset")
    return parsed
