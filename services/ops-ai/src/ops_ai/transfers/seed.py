import argparse
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import system_clock
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.seed import seed_shift
from ops_ai.transfers.models import TransferFact
from scripts.generate_dataset import ScenarioLog, generate
from scripts.transfer_fixture import transfer_decision

INGEST_SOURCE = "seed"
BATCH_SIZE = 500


def seed_transfers(session_factory: sessionmaker[Session], log: ScenarioLog, anchor: datetime) -> int:
    """Loads the fixture's decision record of every p2p transfer at or before the anchor (same shift as the
    payment seed). Idempotent: a re-run inserts nothing and returns 0.
    """
    shift = seed_shift(log.planted, anchor)
    rows: list[dict[str, object]] = []
    for tx in log.transactions:
        decided_at = tx.timestamp + shift
        decision = transfer_decision(tx)
        if decision is None or decided_at > anchor:
            continue
        rows.append(
            {
                "transfer_id": tx.transaction_id,
                "sender_user_id": tx.user_id,
                "amount_minor": tx.amount_minor,
                "currency": tx.currency,
                "method": tx.payment_method,
                "status": tx.status,
                "country_sender": tx.country_sender,
                "country_receiver": tx.country_receiver,
                "reason_code": decision.reason_code,
                "rule_id": decision.rule_id,
                "kyc_tier": decision.kyc_tier,
                "limit_minor": decision.limit_minor,
                "limit_currency": decision.limit_currency,
                "next_tier_limit_minor": decision.next_tier_limit_minor,
                "sender_balance_minor": decision.sender_balance_minor,
                "decided_at": decided_at,
                "ingest_source": INGEST_SOURCE,
            }
        )
    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        statement = (
            insert(TransferFact)
            .values(rows[start : start + BATCH_SIZE])
            .on_conflict_do_nothing(index_elements=[TransferFact.transfer_id])
            .returning(TransferFact.transfer_id)
        )
        with session_factory.begin() as session:
            inserted += len(session.scalars(statement).all())
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    """python -m ops_ai.transfers: prints the inserted count only, never user or transfer ids."""
    parser = argparse.ArgumentParser(prog="python -m ops_ai.transfers")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--records", type=int, default=12000)
    parser.add_argument(
        "--anchor", type=_aware_datetime, default=None, help="ISO 8601 with offset; default now"
    )
    args = parser.parse_args(argv)
    anchor: datetime = args.anchor or system_clock()
    engine = make_engine(load_ops_ai_settings().database_url)
    try:
        inserted = seed_transfers(
            make_session_factory(engine), generate(seed=args.seed, records=args.records), anchor
        )
    finally:
        engine.dispose()
    print(f"transfers: {inserted} inserted")
    return 0


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("anchor needs a UTC offset")
    return parsed
