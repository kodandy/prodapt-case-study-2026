"""KYC facts of P2P senders for the compliance agent, projected from the dataset's p2p transfer records."""

import argparse
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from ops_ai.agents.models import KycProfile
from ops_ai.config import load_ops_ai_settings
from ops_ai.db import make_engine, make_session_factory
from ops_ai.scope import UserScope

if TYPE_CHECKING:
    from scripts.generate_dataset import ScenarioLog

INGEST_SOURCE = "seed"
BATCH_SIZE = 500


@dataclass(frozen=True)
class KycProfileView:
    user_id: uuid.UUID
    kyc_status: str
    country: str


def get_kyc_profile(session: Session, scope: UserScope) -> KycProfileView | None:
    row = session.get(KycProfile, scope.user_id)
    if row is None:
        return None
    return KycProfileView(user_id=row.user_id, kyc_status=row.kyc_status, country=row.country)


def seed_kyc_profiles(session_factory: sessionmaker[Session], log: "ScenarioLog") -> int:
    """One profile per p2p sender from the sender's first record. Existing profiles are kept; a re-run
    inserts nothing and returns 0."""
    first: dict[uuid.UUID, dict[str, object]] = {}
    for tx in log.transactions:
        if tx.record_kind == "p2p_transfer" and tx.user_id not in first:
            first[tx.user_id] = {
                "user_id": tx.user_id,
                "kyc_status": tx.kyc_status,
                "country": tx.country_sender,
                "ingest_source": INGEST_SOURCE,
            }
    rows = list(first.values())
    inserted = 0
    for start in range(0, len(rows), BATCH_SIZE):
        statement = (
            insert(KycProfile)
            .values(rows[start : start + BATCH_SIZE])
            .on_conflict_do_nothing(index_elements=[KycProfile.user_id])
            .returning(KycProfile.user_id)
        )
        with session_factory.begin() as session:
            inserted += len(session.scalars(statement).all())
    return inserted


def main(argv: Sequence[str] | None = None) -> int:
    """python -m ops_ai.agents: prints the inserted count only, never user ids."""
    parser = argparse.ArgumentParser(prog="python -m ops_ai.agents")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--records", type=int, default=12000)
    from scripts.generate_dataset import generate  # seed-only dependency, kept off the API import path

    args = parser.parse_args(argv)
    engine = make_engine(load_ops_ai_settings().database_url)
    try:
        inserted = seed_kyc_profiles(
            make_session_factory(engine), generate(seed=args.seed, records=args.records)
        )
    finally:
        engine.dispose()
    print(f"kyc_profiles: {inserted} inserted")
    return 0
