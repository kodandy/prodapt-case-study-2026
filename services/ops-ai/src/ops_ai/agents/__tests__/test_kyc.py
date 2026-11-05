"""Tests for the KYC profile projection (`seed_kyc_profiles`, `get_kyc_profile`), written from
docs/specs/r2-agents.md section 4 (`kyc.py`)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.agents.kyc import KycProfileView, get_kyc_profile, seed_kyc_profiles
from ops_ai.scope import UserScope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from scripts.generate_dataset import (
    CanonicalTransaction,
    KycStatus,
    PlantedScenarios,
    RecordKind,
    ScenarioLog,
    generate,
)

RECORDS = 2000
USER_A = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
USER_B = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
BASE = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)

SELECT_PROFILES = text(
    "SELECT user_id, kyc_status, country, ingest_source FROM ops_ai.kyc_profiles ORDER BY user_id"
)
INSERT_PROFILE = text(
    "INSERT INTO ops_ai.kyc_profiles (user_id, kyc_status, country, ingest_source) VALUES "
    "(:user_id, :kyc_status, :country, 'seed')"
)


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate(seed=42, records=RECORDS)


def _profiles(session_factory: sessionmaker[Session]) -> list[dict[str, object]]:
    with session_factory() as session:
        return [dict(row) for row in session.execute(SELECT_PROFILES).mappings()]


def _expected_profiles(log: ScenarioLog) -> dict[uuid.UUID, tuple[str, str]]:
    first: dict[uuid.UUID, tuple[str, str]] = {}
    for tx in log.transactions:
        if tx.record_kind == "p2p_transfer":
            first.setdefault(tx.user_id, (tx.kyc_status, tx.country_sender))
    return first


def _tx(
    n: int,
    user_id: uuid.UUID,
    *,
    record_kind: RecordKind = "p2p_transfer",
    kyc_status: KycStatus = "verified",
    country: str = "US",
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=uuid.UUID(int=n),
        user_id=user_id,
        merchant_id="mch_0001" if record_kind == "merchant_payment" else None,
        amount_minor=10_000,
        currency="USD",
        payment_method="card" if record_kind == "merchant_payment" else "bank_transfer",
        status="success",
        failure_reason=None,
        fraud_score=0.1,
        timestamp=BASE + timedelta(minutes=n),
        chargeback_flag=False,
        settlement_status="pending",
        resolution_notes=None,
        kyc_status=kyc_status,
        country_sender=country,
        country_receiver="IN",
        record_kind=record_kind,
        scenario="baseline",
        card_bin=None,
        acquirer=None,
        rule_id=None,
        sender_balance_minor=None,
    )


def _small_log(*transactions: CanonicalTransaction) -> ScenarioLog:
    planted = PlantedScenarios(
        sq1_merchant_id="mch_0001",
        sq1_window_start=BASE,
        sq1_window_end=BASE + timedelta(hours=6),
        sq1_card_bin="400000",
        sq2_transaction_id=uuid.UUID(int=2),
        sq3_merchant_id="mch_0002",
        sq3_transaction_ids=(uuid.UUID(int=3),),
        sq4_merchant_id="mch_0003",
    )
    return ScenarioLog(transactions=transactions, incidents=(), planted=planted)


# --- seed_kyc_profiles -----------------------------------------------------------------------------------


def test_seed_inserts_one_profile_per_p2p_sender_of_the_generated_log(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange
    expected = _expected_profiles(log)

    # Act
    inserted = seed_kyc_profiles(session_factory, log)

    # Assert
    assert inserted == len(expected)
    assert len(expected) > 0
    assert _profiles(session_factory) == [
        {"user_id": user_id, "kyc_status": status, "country": country, "ingest_source": "seed"}
        for user_id, (status, country) in sorted(expected.items())
    ]


def test_seed_rerun_inserts_nothing_and_returns_zero(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange
    first = seed_kyc_profiles(session_factory, log)
    before = _profiles(session_factory)

    # Act
    second = seed_kyc_profiles(session_factory, log)

    # Assert
    assert first == len(before)
    assert second == 0
    assert _profiles(session_factory) == before


def test_seed_skips_merchant_payment_senders(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    log = _small_log(
        _tx(1, USER_A, record_kind="p2p_transfer"),
        _tx(2, USER_B, record_kind="merchant_payment"),
    )

    # Act
    inserted = seed_kyc_profiles(session_factory, log)

    # Assert
    assert inserted == 1
    assert [row["user_id"] for row in _profiles(session_factory)] == [USER_A]


def test_seed_takes_the_first_record_of_a_sender_when_later_records_disagree(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    log = _small_log(
        _tx(1, USER_A, kyc_status="pending", country="GB"),
        _tx(2, USER_A, kyc_status="verified", country="US"),
        _tx(3, USER_A, kyc_status="failed", country="IN"),
    )

    # Act
    inserted = seed_kyc_profiles(session_factory, log)

    # Assert
    assert inserted == 1
    assert _profiles(session_factory) == [
        {"user_id": USER_A, "kyc_status": "pending", "country": "GB", "ingest_source": "seed"}
    ]


def test_seed_keeps_an_existing_profile_and_inserts_only_the_missing_senders(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        session.execute(INSERT_PROFILE, {"user_id": USER_A, "kyc_status": "failed", "country": "IN"})
    log = _small_log(_tx(1, USER_A, kyc_status="verified", country="US"), _tx(2, USER_B, country="DE"))

    # Act
    inserted = seed_kyc_profiles(session_factory, log)

    # Assert
    assert inserted == 1
    assert _profiles(session_factory) == [
        {"user_id": USER_A, "kyc_status": "failed", "country": "IN", "ingest_source": "seed"},
        {"user_id": USER_B, "kyc_status": "verified", "country": "DE", "ingest_source": "seed"},
    ]


def test_seed_of_a_log_without_p2p_senders_returns_zero(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    log = _small_log(_tx(1, USER_A, record_kind="merchant_payment"))

    # Act
    inserted = seed_kyc_profiles(session_factory, log)

    # Assert
    assert inserted == 0
    assert _profiles(session_factory) == []


def test_the_planted_sq2_sender_is_verified_and_in_the_us(
    session_factory: sessionmaker[Session], log: ScenarioLog
) -> None:
    # Arrange
    sq2 = next(tx for tx in log.transactions if tx.transaction_id == log.planted.sq2_transaction_id)
    seed_kyc_profiles(session_factory, log)

    # Act
    with session_factory() as session:
        profile = get_kyc_profile(session, UserScope(user_id=sq2.user_id))

    # Assert
    assert profile == KycProfileView(user_id=sq2.user_id, kyc_status="verified", country="US")


# --- get_kyc_profile -------------------------------------------------------------------------------------


def test_get_kyc_profile_returns_the_scope_users_own_profile(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        session.execute(INSERT_PROFILE, {"user_id": USER_A, "kyc_status": "pending", "country": "GB"})
        session.execute(INSERT_PROFILE, {"user_id": USER_B, "kyc_status": "verified", "country": "US"})

    # Act
    with session_factory() as session:
        profile = get_kyc_profile(session, UserScope(user_id=USER_A))

    # Assert
    assert profile == KycProfileView(user_id=USER_A, kyc_status="pending", country="GB")


def test_get_kyc_profile_never_returns_another_users_profile(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        session.execute(INSERT_PROFILE, {"user_id": USER_B, "kyc_status": "verified", "country": "US"})

    # Act
    with session_factory() as session:
        profile = get_kyc_profile(session, UserScope(user_id=USER_A))

    # Assert
    assert profile is None


def test_get_kyc_profile_on_an_empty_table_is_none(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    scope = UserScope(user_id=USER_A)

    # Act
    with session_factory() as session:
        profile = get_kyc_profile(session, scope)

    # Assert
    assert profile is None
