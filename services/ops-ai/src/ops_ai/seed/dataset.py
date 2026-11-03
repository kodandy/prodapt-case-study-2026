import argparse
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import islice

from contracts.events import PaymentEventV1
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.clock import Clock, system_clock
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.ingest.consumer import apply_payment_event
from ops_ai.investigation.models import IncidentRecord
from ops_ai.investigation.taxonomy import failure_code
from scripts.generate_dataset import CanonicalTransaction, Incident, PlantedScenarios, ScenarioLog, generate

MERCHANT_NAMESPACE = uuid.UUID("acc393ed-647c-4a3b-8adc-f53e5d7b1fe3")
"""Shared with the gateway side through scripts/demo.py; changing it orphans every seeded merchant."""
BATCH_SIZE = 500


@dataclass(frozen=True)
class SeedResult:
    payments_applied: int
    payments_skipped: int
    """Records of any kind whose shifted time is after the anchor."""
    incidents_upserted: int


def merchant_uuid(external_id: str) -> uuid.UUID:
    return uuid.uuid5(MERCHANT_NAMESPACE, external_id)


def seed_shift(planted: PlantedScenarios, anchor: datetime) -> timedelta:
    """Moves the planted SQ1 afternoon so that it ends at the anchor."""
    return anchor - planted.sq1_window_end


def to_payment_event(tx: CanonicalTransaction, shift: timedelta) -> PaymentEventV1 | None:
    """None for anything but a merchant card payment."""
    if tx.record_kind != "merchant_payment" or tx.payment_method != "card" or tx.merchant_id is None:
        return None
    if tx.status in ("success", "reversed"):
        event_type, state, decline_code = "payment.succeeded", "succeeded", None
    elif tx.status in ("failed", "flagged"):
        event_type, state, decline_code = (
            "payment.declined",
            "declined",
            failure_code(tx.failure_reason or ""),
        )
    else:
        event_type, state, decline_code = "payment.unknown", "unknown", None
    return PaymentEventV1.model_validate(
        {
            "event_id": uuid.uuid5(MERCHANT_NAMESPACE, f"seed:{tx.transaction_id}"),
            "event_type": event_type,
            "occurred_at": tx.timestamp + shift,
            "origin": "backfill",
            "entity_version": 1,
            "payment_id": tx.transaction_id,
            "merchant_id": merchant_uuid(tx.merchant_id),
            "amount_minor": tx.amount_minor,
            "currency": tx.currency,
            "method": "card",
            "state": state,
            "provider": tx.acquirer,
            "decline_code": decline_code,
            "card_bin": tx.card_bin,
            "card_last4": None,
        }
    )


def seed_dataset(
    session_factory: sessionmaker[Session], clock: Clock, log: ScenarioLog, anchor: datetime
) -> SeedResult:
    """Replays the log through the consumer projection, one transaction per batch, then upserts the incidents.

    Idempotent: payments dedupe on event_id, so a re-run keeps the first anchor's history.
    """
    shift = seed_shift(log.planted, anchor)
    kept = [tx for tx in log.transactions if tx.timestamp + shift <= anchor]
    events = [event for event in (to_payment_event(tx, shift) for tx in kept) if event is not None]
    applied = 0
    for batch in _batches(events, BATCH_SIZE):
        with session_factory.begin() as session:
            applied += sum(apply_payment_event(session, clock, event) for event in batch)
    with session_factory.begin() as session:
        for incident in log.incidents:
            _upsert_incident(session, incident, shift)
    return SeedResult(
        payments_applied=applied,
        payments_skipped=len(log.transactions) - len(kept),
        incidents_upserted=len(log.incidents),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """python -m ops_ai.seed: prints the three counts only, never merchant data."""
    parser = argparse.ArgumentParser(prog="python -m ops_ai.seed")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--records", type=int, default=12000)
    parser.add_argument(
        "--anchor", type=_aware_datetime, default=None, help="ISO 8601 with offset; default now"
    )
    args = parser.parse_args(argv)
    anchor: datetime = args.anchor or system_clock()
    engine = make_engine(load_ops_ai_settings().database_url)
    try:
        result = seed_dataset(
            make_session_factory(engine), system_clock, generate(seed=args.seed, records=args.records), anchor
        )
    finally:
        engine.dispose()
    print(
        f"seed: {result.payments_applied} payments applied, {result.payments_skipped} skipped, "
        f"{result.incidents_upserted} incidents upserted"
    )
    return 0


def _aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("anchor needs a UTC offset")
    return parsed


def _batches(events: Iterable[PaymentEventV1], size: int) -> Iterator[list[PaymentEventV1]]:
    iterator = iter(events)
    while batch := list(islice(iterator, size)):
        yield batch


def _upsert_incident(session: Session, incident: Incident, shift: timedelta) -> None:
    values = {
        "incident_id": incident.incident_id,
        "merchant_id": None if incident.merchant_id is None else merchant_uuid(incident.merchant_id),
        "title": incident.title,
        "root_cause": incident.root_cause,
        "fix_applied": incident.fix_applied,
        "resolution_notes": incident.resolution_notes,
        "started_at": incident.started_at + shift,
        "resolved_at": None if incident.resolved_at is None else incident.resolved_at + shift,
        "recurrence_within_7d": incident.recurrence_within_7d,
    }
    statement = insert(IncidentRecord).values(values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[IncidentRecord.incident_id],
            set_={key: statement.excluded[key] for key in values if key != "incident_id"},
        )
    )
