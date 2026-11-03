"""Golden tests for the planted SQ3 burst (docs/specs/sq3.md section 4 Golden and section 5)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.llm_port import MockLlm
from ops_ai.db import make_session_factory
from ops_ai.fraud_watch import CardTestingEpisode, analyze_fraud
from ops_ai.fraud_watch.service import FraudInvestigation, investigate_fraud
from ops_ai.scope import Scope
from ops_ai.seed import SeedResult, merchant_uuid, seed_dataset
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from conftest import DATA_SCHEMAS
from scripts.generate_dataset import ScenarioLog, generate

ANCHOR = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
QUESTION = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"
SQ3_MERCHANT = "mch_0002"
WHOLE_HISTORY_START = ANCHOR - timedelta(days=60)
WHOLE_HISTORY_END = ANCHOR + timedelta(hours=1)


@dataclass(frozen=True)
class Golden:
    session_factory: sessionmaker[Session]
    log: ScenarioLog
    result: SeedResult


@pytest.fixture(scope="module")
def golden(engine: Engine) -> Golden:
    """Seeds generate(seed=42) once per module; function-scoped fixtures of other modules truncate it."""
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT format('%I.%I', schemaname, tablename) FROM pg_tables "
                "WHERE schemaname = ANY(:schemas) AND tablename <> 'alembic_version'"
            ),
            {"schemas": list(DATA_SCHEMAS)},
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    session_factory = make_session_factory(engine)
    log = generate(seed=42)
    result = seed_dataset(session_factory, lambda: ANCHOR, log, ANCHOR)
    return Golden(session_factory=session_factory, log=log, result=result)


def _flagged_episodes(
    golden: Golden, merchant: str, start: datetime, end: datetime
) -> tuple[CardTestingEpisode, ...]:
    with golden.session_factory() as session:
        analysis = analyze_fraud(session, Scope(merchant_id=merchant_uuid(merchant)), start, end)
    return tuple(episode for episode in analysis.episodes if episode.flagged)


def _count(golden: Golden, table: str) -> int:
    queries = {
        "alerts": "SELECT count(*) FROM ops_ai.fraud_alerts",
        "tickets": "SELECT count(*) FROM ops_ai.tickets",
        "card_refs": "SELECT count(*) FROM ops_ai.payment_facts WHERE card_ref IS NOT NULL",
    }
    with golden.session_factory() as session:
        return int(session.execute(text(queries[table])).scalar_one())


def _investigate(golden: Golden, merchant: str = SQ3_MERCHANT) -> FraudInvestigation:
    with golden.session_factory() as session:
        return investigate_fraud(
            session, Scope(merchant_id=merchant_uuid(merchant)), QUESTION, ANCHOR, MockLlm()
        )


def test_golden_the_sq3_merchant_has_exactly_one_flagged_episode_in_the_last_24_hours(golden: Golden) -> None:
    # Arrange / Act
    flagged = _flagged_episodes(golden, SQ3_MERCHANT, ANCHOR - timedelta(hours=24), ANCHOR)

    # Assert
    assert len(flagged) == 1


def test_golden_the_flagged_episode_holds_exactly_the_planted_payment_ids(golden: Golden) -> None:
    # Arrange
    planted_ids = golden.log.planted.sq3_transaction_ids

    # Act
    (episode,) = _flagged_episodes(golden, SQ3_MERCHANT, ANCHOR - timedelta(hours=24), ANCHOR)

    # Assert
    assert set(episode.payment_ids) == set(planted_ids)
    assert len(episode.payment_ids) == len(planted_ids)
    assert episode.micro_payments == len(planted_ids)
    assert len(planted_ids) >= 30


def test_golden_the_flagged_episode_has_unknown_cards_all_declined_and_the_specified_reasons(
    golden: Golden,
) -> None:
    # Arrange / Act
    (episode,) = _flagged_episodes(golden, SQ3_MERCHANT, ANCHOR - timedelta(hours=24), ANCHOR)

    # Assert
    assert episode.distinct_cards is None
    assert episode.decline_ratio == 1.0
    assert episode.reasons == ("micro_burst", "card_identity_unknown", "high_decline_ratio")


def test_golden_the_flagged_episode_lists_six_digit_bins_and_only_usd(golden: Golden) -> None:
    # Arrange / Act
    (episode,) = _flagged_episodes(golden, SQ3_MERCHANT, ANCHOR - timedelta(hours=24), ANCHOR)

    # Assert
    assert len(episode.card_bins) >= 1
    assert [bin_ for bin_ in episode.card_bins if len(bin_) != 6 or not bin_.isdigit()] == []
    assert episode.currencies == ("USD",)


def test_golden_the_flagged_episode_spans_less_than_ten_minutes_and_the_share_is_at_least_80_percent(
    golden: Golden,
) -> None:
    # Arrange / Act
    (episode,) = _flagged_episodes(golden, SQ3_MERCHANT, ANCHOR - timedelta(hours=24), ANCHOR)

    # Assert
    assert episode.end - episode.start < timedelta(minutes=10)
    assert episode.micro_share >= 0.8


def test_golden_no_other_seeded_merchant_has_a_flagged_episode_in_the_whole_history(golden: Golden) -> None:
    # Arrange
    others = sorted(
        {
            tx.merchant_id
            for tx in golden.log.transactions
            if tx.merchant_id is not None and tx.merchant_id != SQ3_MERCHANT
        }
    )

    # Act
    flagged = {m: _flagged_episodes(golden, m, WHOLE_HISTORY_START, WHOLE_HISTORY_END) for m in others}

    # Assert
    assert len(others) >= 3
    assert {m: episodes for m, episodes in flagged.items() if episodes} == {}


def test_golden_seeding_creates_no_alert_no_ticket_and_no_card_ref(golden: Golden) -> None:
    # Arrange / Act
    counts = (_count(golden, "alerts"), _count(golden, "tickets"), _count(golden, "card_refs"))

    # Assert
    assert golden.result.payments_applied > 0
    assert counts == (0, 0, 0)


def test_golden_investigate_fraud_flags_card_testing_with_the_planted_ids_and_no_alert(
    golden: Golden,
) -> None:
    # Arrange
    planted_ids = golden.log.planted.sq3_transaction_ids

    # Act
    result = _investigate(golden)

    # Assert
    assert result.intent == "card_testing"
    assert result.cause_label == "card_testing"
    assert result.analysis is not None
    flagged = [episode for episode in result.analysis.episodes if episode.flagged]
    assert [set(episode.payment_ids) for episode in flagged] == [set(planted_ids)]
    assert result.alerts == ()
    assert [call.name for call in result.tool_calls] == ["analyze_fraud"]


def test_golden_investigate_fraud_cites_the_episode_and_says_the_card_identity_is_unknown(
    golden: Golden,
) -> None:
    # Arrange / Act
    result = _investigate(golden)

    # Assert
    assert result.analysis is not None
    (episode,) = [e for e in result.analysis.episodes if e.flagged]
    refs = [item.ref for item in result.evidence]
    assert refs[0] == f"episode:{episode.first_payment_id}"
    assert "card identity unknown" in result.evidence[0].summary
    assert [ref for ref in refs if ref.startswith("alert:")] == []
    assert len([ref for ref in refs if ref.startswith("payment:")]) == 5
    assert (result.mode, result.model) == ("llm", "mock")


def test_golden_another_merchant_sees_no_card_testing(golden: Golden) -> None:
    # Arrange / Act
    result = _investigate(golden, "mch_0001")

    # Assert
    assert result.cause_label == "no_card_testing"
    assert result.suggested_fixes == ()
    assert result.alerts == ()
    assert result.evidence == ()


def test_golden_the_planted_burst_ids_are_the_seeded_payment_ids(golden: Golden) -> None:
    # Arrange
    planted_ids = set(golden.log.planted.sq3_transaction_ids)

    # Act
    with golden.session_factory() as session:
        stored = {
            row.payment_id: row.merchant_id
            for row in session.execute(
                text("SELECT payment_id, merchant_id FROM ops_ai.payment_facts WHERE payment_id = ANY(:ids)"),
                {"ids": list(planted_ids)},
            )
        }

    # Assert
    assert set(stored) == planted_ids
    assert set(stored.values()) == {merchant_uuid(SQ3_MERCHANT)}
