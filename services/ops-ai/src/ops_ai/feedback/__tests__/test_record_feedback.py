"""Tests for ops_ai.feedback.service.record_feedback and incident_scores (docs/specs/r2-agents.md
section 1): verdict rules, target visibility, one vote per target, and the effects on the fraud rule
versions and the incident scores."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from gateway.clock import FixedClock
from ops_ai.feedback.service import (
    VERDICTS_BY_KIND,
    FeedbackRejectedError,
    FeedbackResult,
    IncidentScore,
    TargetKind,
    Verdict,
    incident_scores,
    record_feedback,
)
from ops_ai.feedback.thresholds import MAX_STEPS, RuleVersion, active_rule_version, rule_history
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)
STARTED_AT = datetime(2026, 10, 30, 8, 0, tzinfo=UTC)

INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :window_key, :merchant_id, "
    "'card_testing', :first_payment_id, :started_at, :started_at, 12, '{}'::jsonb, :started_at)"
)
INSERT_INCIDENT = text(
    "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, fix_applied, "
    "resolution_notes, started_at, resolved_at, recurrence_within_7d) VALUES "
    "(:incident_id, :merchant_id, 'Issuer outage', 'Issuer timeouts', 'Rerouted', 'notes', :started_at, "
    ":started_at, false)"
)
INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES (:merchant_id, :version, "
    ":steps, :min_micro_payments, :min_micro_share, :min_distinct_cards, :reason, NULL, :created_at)"
)
INSERT_SCORE = text(
    "INSERT INTO ops_ai.incident_feedback_scores (merchant_id, incident_id, helpful, not_helpful, "
    "updated_at) VALUES (:merchant_id, :incident_id, :helpful, :not_helpful, :updated_at)"
)
LADDER = {
    0: (10, 0.8, 3),
    1: (12, 0.83, 4),
    2: (14, 0.86, 5),
    3: (16, 0.89, 6),
    4: (18, 0.92, 7),
    5: (20, 0.95, 8),
}


def _insert_alert(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> uuid.UUID:
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_ALERT,
            {
                "alert_id": alert_id,
                "window_key": f"card_testing:{merchant_id}:{alert_id}",
                "merchant_id": merchant_id,
                "first_payment_id": uuid.uuid4(),
                "started_at": STARTED_AT,
            },
        )
    return alert_id


def _insert_incident(
    session_factory: sessionmaker[Session], incident_id: str, merchant_id: uuid.UUID | None
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_INCIDENT,
            {"incident_id": incident_id, "merchant_id": merchant_id, "started_at": STARTED_AT},
        )


def _insert_versions_up_to(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID, steps: int
) -> None:
    """One version row per step 1..steps (version n has n steps)."""
    with session_factory.begin() as session:
        for version in range(1, steps + 1):
            payments, share, cards = LADDER[version]
            session.execute(
                INSERT_VERSION,
                {
                    "merchant_id": merchant_id,
                    "version": version,
                    "steps": version,
                    "min_micro_payments": payments,
                    "min_micro_share": share,
                    "min_distinct_cards": cards,
                    "reason": "feedback:false_positive",
                    "created_at": TEST_NOW - timedelta(days=10 - version),
                },
            )


def _insert_score(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    incident_id: str,
    helpful: int,
    not_helpful: int,
) -> None:
    with session_factory.begin() as session:
        session.execute(
            INSERT_SCORE,
            {
                "merchant_id": merchant_id,
                "incident_id": incident_id,
                "helpful": helpful,
                "not_helpful": not_helpful,
                "updated_at": STARTED_AT,
            },
        )


def _record(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    target_kind: TargetKind,
    target_id: str,
    verdict: Verdict,
    comment: str | None = None,
    scope: Scope = SCOPE,
) -> FeedbackResult:
    with session_factory.begin() as session:
        return record_feedback(session, scope, clock, target_kind, target_id, verdict, comment)


def _rejection(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    target_kind: TargetKind,
    target_id: str,
    verdict: Verdict,
    scope: Scope = SCOPE,
) -> str:
    with pytest.raises(FeedbackRejectedError) as raised, session_factory.begin() as session:
        record_feedback(session, scope, clock, target_kind, target_id, verdict, None)
    return raised.value.reason


def _feedback_rows(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT
) -> list[tuple[uuid.UUID, str, str, str, str | None, datetime]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT feedback_id, target_kind, target_id, verdict, comment, created_at "
                "FROM ops_ai.feedback WHERE merchant_id = :merchant_id ORDER BY created_at, target_id"
            ),
            {"merchant_id": merchant_id},
        ).all()
    return [
        (row.feedback_id, row.target_kind, row.target_id, row.verdict, row.comment, row.created_at)
        for row in rows
    ]


def _history(session_factory: sessionmaker[Session], scope: Scope = SCOPE) -> list[RuleVersion]:
    with session_factory() as session:
        return rule_history(session, scope)


def _score_rows(
    session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT
) -> list[tuple[str, int, int]]:
    with session_factory() as session:
        rows = session.execute(
            text(
                "SELECT incident_id, helpful, not_helpful FROM ops_ai.incident_feedback_scores "
                "WHERE merchant_id = :merchant_id ORDER BY incident_id"
            ),
            {"merchant_id": merchant_id},
        ).all()
    return [(row.incident_id, row.helpful, row.not_helpful) for row in rows]


# --- the verdict table ----------------------------------------------------------------------------------


def test_verdicts_by_kind_maps_each_target_kind_to_its_two_verdicts() -> None:
    # Arrange / Act
    table = dict(VERDICTS_BY_KIND)

    # Assert
    assert table == {
        "alert": ("true_positive", "false_positive"),
        "answer": ("helpful", "not_helpful"),
        "incident": ("helpful", "not_helpful"),
    }


# --- rule 1: verdict_mismatch ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("target_kind", "verdict"),
    [
        ("alert", "helpful"),
        ("alert", "not_helpful"),
        ("answer", "true_positive"),
        ("answer", "false_positive"),
        ("incident", "true_positive"),
        ("incident", "false_positive"),
    ],
)
def test_a_verdict_that_does_not_belong_to_the_target_kind_is_a_verdict_mismatch(
    session_factory: sessionmaker[Session],
    clock: FixedClock,
    target_kind: TargetKind,
    verdict: Verdict,
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory, MERCHANT)
    _insert_incident(session_factory, "INC-1", MERCHANT)
    target_ids: dict[str, str] = {"alert": str(alert_id), "answer": "answer-1", "incident": "INC-1"}

    # Act
    reason = _rejection(session_factory, clock, target_kind, target_ids[target_kind], verdict)

    # Assert
    assert reason == "verdict_mismatch"
    assert _feedback_rows(session_factory) == []
    assert _history(session_factory) == []
    assert _score_rows(session_factory) == []


def test_verdict_mismatch_is_reported_before_an_unknown_target(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    missing_alert = str(uuid.uuid4())

    # Act
    reason = _rejection(session_factory, clock, "alert", missing_alert, "helpful")

    # Assert
    assert reason == "verdict_mismatch"


# --- rule 2: unknown_target -----------------------------------------------------------------------------


def test_an_alert_of_another_merchant_is_an_unknown_target(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    others_alert = _insert_alert(session_factory, OTHER_MERCHANT)

    # Act
    reason = _rejection(session_factory, clock, "alert", str(others_alert), "false_positive")

    # Assert
    assert reason == "unknown_target"
    assert _feedback_rows(session_factory) == []
    assert _history(session_factory) == []
    assert _history(session_factory, OTHER_SCOPE) == []


def test_an_alert_id_that_is_not_a_uuid_is_an_unknown_target(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_alert(session_factory, MERCHANT)

    # Act
    reason = _rejection(session_factory, clock, "alert", "not-a-uuid", "false_positive")

    # Assert
    assert reason == "unknown_target"


def test_a_well_formed_alert_id_without_a_row_is_an_unknown_target(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    missing_alert = str(uuid.uuid4())

    # Act
    reason = _rejection(session_factory, clock, "alert", missing_alert, "true_positive")

    # Assert
    assert reason == "unknown_target"


def test_an_incident_of_another_merchant_is_an_unknown_target(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-OTHER", OTHER_MERCHANT)

    # Act
    reason = _rejection(session_factory, clock, "incident", "INC-OTHER", "helpful")

    # Assert
    assert reason == "unknown_target"
    assert _feedback_rows(session_factory) == []
    assert _score_rows(session_factory) == []


def test_an_incident_id_without_a_row_is_an_unknown_target(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    reason = _rejection(session_factory, clock, "incident", "INC-404", "not_helpful")

    # Assert
    assert reason == "unknown_target"


def test_an_unknown_target_leaves_no_trace_in_the_other_merchants_data(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    others_alert = _insert_alert(session_factory, OTHER_MERCHANT)

    # Act
    _rejection(session_factory, clock, "alert", str(others_alert), "false_positive")

    # Assert
    assert _feedback_rows(session_factory, OTHER_MERCHANT) == []


def test_an_answer_accepts_any_id_including_an_opaque_correlation_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    correlation_id = str(uuid.uuid4())

    # Act
    result = _record(session_factory, clock, "answer", correlation_id, "helpful")

    # Assert
    assert (result.target_kind, result.target_id, result.verdict) == ("answer", correlation_id, "helpful")


# --- rule 3: duplicate ----------------------------------------------------------------------------------


def test_a_second_feedback_on_the_same_alert_is_a_duplicate_and_does_not_move_the_thresholds_again(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))
    first = _record(session_factory, clock, "alert", alert_id, "false_positive")

    # Act
    reason = _rejection(session_factory, clock, "alert", alert_id, "false_positive")

    # Assert
    assert reason == "duplicate"
    assert [row[0] for row in _feedback_rows(session_factory)] == [first.feedback_id]
    assert [rule.version for rule in _history(session_factory)] == [1]


def test_the_opposite_verdict_on_the_same_alert_is_still_a_duplicate(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))
    _record(session_factory, clock, "alert", alert_id, "false_positive")

    # Act
    reason = _rejection(session_factory, clock, "alert", alert_id, "true_positive")

    # Assert
    assert reason == "duplicate"
    with session_factory() as session:
        assert active_rule_version(session, SCOPE).steps == 1


def test_a_second_feedback_on_the_same_incident_is_a_duplicate_and_the_counts_stay(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)
    _record(session_factory, clock, "incident", "INC-1", "helpful")

    # Act
    reason = _rejection(session_factory, clock, "incident", "INC-1", "helpful")

    # Assert
    assert reason == "duplicate"
    assert _score_rows(session_factory) == [("INC-1", 1, 0)]


def test_a_second_feedback_on_the_same_answer_is_a_duplicate(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _record(session_factory, clock, "answer", "corr-1", "helpful")

    # Act
    reason = _rejection(session_factory, clock, "answer", "corr-1", "not_helpful")

    # Assert
    assert reason == "duplicate"
    assert len(_feedback_rows(session_factory)) == 1


def test_the_same_answer_id_may_be_rated_by_another_merchant(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _record(session_factory, clock, "answer", "shared-corr", "helpful")

    # Act
    result = _record(session_factory, clock, "answer", "shared-corr", "not_helpful", scope=OTHER_SCOPE)

    # Assert
    assert result.verdict == "not_helpful"
    assert len(_feedback_rows(session_factory)) == 1
    assert len(_feedback_rows(session_factory, OTHER_MERCHANT)) == 1


def test_the_same_id_may_be_rated_as_an_answer_and_as_an_incident(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)
    _record(session_factory, clock, "answer", "INC-1", "helpful")

    # Act
    result = _record(session_factory, clock, "incident", "INC-1", "helpful")

    # Assert
    assert result.target_kind == "incident"
    assert len(_feedback_rows(session_factory)) == 2


# --- rule 4: false_positive / true_positive -------------------------------------------------------------


def test_a_false_positive_raises_the_steps_and_writes_version_one_with_reason_and_feedback_id(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "false_positive", "just a load test")

    # Assert
    assert result.rule_version == RuleVersion(
        version=1,
        steps=1,
        min_micro_payments=12,
        min_micro_share=0.83,
        min_distinct_cards=4,
        reason="feedback:false_positive",
        feedback_id=result.feedback_id,
        created_at=TEST_NOW,
    )
    assert result.incident_score is None
    assert (result.target_kind, result.target_id, result.verdict) == ("alert", alert_id, "false_positive")
    assert _history(session_factory) == [result.rule_version]


def test_the_feedback_row_is_stored_with_its_comment_and_the_clock_time(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "false_positive", "just a load test")

    # Assert
    assert _feedback_rows(session_factory) == [
        (result.feedback_id, "alert", alert_id, "false_positive", "just a load test", TEST_NOW)
    ]


def test_a_feedback_without_a_comment_stores_null(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange / Act
    result = _record(session_factory, clock, "answer", "corr-1", "helpful", None)

    # Assert
    assert _feedback_rows(session_factory) == [
        (result.feedback_id, "answer", "corr-1", "helpful", None, TEST_NOW)
    ]


def test_each_false_positive_on_a_different_alert_raises_the_steps_by_one(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alerts = [str(_insert_alert(session_factory, MERCHANT)) for _ in range(3)]

    # Act
    results = [_record(session_factory, clock, "alert", alert, "false_positive") for alert in alerts]

    # Assert
    versions = [result.rule_version for result in results]
    assert [(v.version, v.steps) if v else None for v in versions] == [(1, 1), (2, 2), (3, 3)]
    assert versions[2] is not None
    assert (versions[2].min_micro_payments, versions[2].min_micro_share, versions[2].min_distinct_cards) == (
        16,
        0.89,
        6,
    )
    assert [rule.version for rule in _history(session_factory)] == [3, 2, 1]


def test_a_true_positive_at_the_baseline_writes_no_version(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "true_positive")

    # Assert
    assert result.rule_version is None
    assert result.incident_score is None
    assert _history(session_factory) == []
    assert [(row[1], row[2], row[3]) for row in _feedback_rows(session_factory)] == [
        ("alert", alert_id, "true_positive")
    ]


def test_a_true_positive_after_a_false_positive_steps_back_and_writes_a_new_version(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    first_alert = str(_insert_alert(session_factory, MERCHANT))
    second_alert = str(_insert_alert(session_factory, MERCHANT))
    _record(session_factory, clock, "alert", first_alert, "false_positive")

    # Act
    result = _record(session_factory, clock, "alert", second_alert, "true_positive")

    # Assert
    assert result.rule_version == RuleVersion(
        version=2,
        steps=0,
        min_micro_payments=10,
        min_micro_share=0.8,
        min_distinct_cards=3,
        reason="feedback:true_positive",
        feedback_id=result.feedback_id,
        created_at=TEST_NOW,
    )
    with session_factory() as session:
        assert active_rule_version(session, SCOPE) == result.rule_version


def test_the_steps_start_from_the_active_version_not_from_the_baseline(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_versions_up_to(session_factory, MERCHANT, 3)
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "false_positive")

    # Assert
    assert result.rule_version is not None
    assert (result.rule_version.version, result.rule_version.steps) == (4, 4)
    assert result.rule_version.min_micro_payments == 18


def test_a_true_positive_steps_down_from_the_active_version(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_versions_up_to(session_factory, MERCHANT, 3)
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "true_positive")

    # Assert
    assert result.rule_version is not None
    assert (result.rule_version.version, result.rule_version.steps) == (4, 2)
    assert result.rule_version.reason == "feedback:true_positive"


def test_a_false_positive_at_max_steps_clamps_and_writes_no_new_version(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_versions_up_to(session_factory, MERCHANT, MAX_STEPS)
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "false_positive")

    # Assert
    assert result.rule_version is None
    assert [rule.version for rule in _history(session_factory)] == [5, 4, 3, 2, 1]
    assert len(_feedback_rows(session_factory)) == 1


def test_a_false_positive_on_one_merchant_never_changes_another_merchants_rules(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    _record(session_factory, clock, "alert", alert_id, "false_positive")

    # Assert
    assert _history(session_factory, OTHER_SCOPE) == []
    with session_factory() as session:
        assert active_rule_version(session, OTHER_SCOPE).version == 0


def test_the_other_merchants_steps_do_not_leak_into_the_starting_point(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_versions_up_to(session_factory, OTHER_MERCHANT, 4)
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    result = _record(session_factory, clock, "alert", alert_id, "false_positive")

    # Assert
    assert result.rule_version is not None
    assert (result.rule_version.version, result.rule_version.steps) == (1, 1)
    assert len(_history(session_factory, OTHER_SCOPE)) == 4


# --- rule 4: incident and answer feedback ---------------------------------------------------------------


def test_helpful_on_an_incident_creates_a_score_row_with_one_helpful(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)

    # Act
    result = _record(session_factory, clock, "incident", "INC-1", "helpful")

    # Assert
    assert result.incident_score == IncidentScore(incident_id="INC-1", helpful=1, not_helpful=0)
    assert result.rule_version is None
    assert _score_rows(session_factory) == [("INC-1", 1, 0)]
    assert _history(session_factory) == []


def test_not_helpful_on_an_incident_increments_the_not_helpful_counter(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)

    # Act
    result = _record(session_factory, clock, "incident", "INC-1", "not_helpful")

    # Assert
    assert result.incident_score == IncidentScore(incident_id="INC-1", helpful=0, not_helpful=1)
    assert _score_rows(session_factory) == [("INC-1", 0, 1)]


def test_helpful_upserts_into_an_existing_score_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)
    _insert_score(session_factory, MERCHANT, "INC-1", helpful=2, not_helpful=1)

    # Act
    result = _record(session_factory, clock, "incident", "INC-1", "helpful")

    # Assert
    assert result.incident_score == IncidentScore(incident_id="INC-1", helpful=3, not_helpful=1)
    assert _score_rows(session_factory) == [("INC-1", 3, 1)]


def test_not_helpful_upserts_into_an_existing_score_row(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)
    _insert_score(session_factory, MERCHANT, "INC-1", helpful=2, not_helpful=1)

    # Act
    result = _record(session_factory, clock, "incident", "INC-1", "not_helpful")

    # Assert
    assert result.incident_score == IncidentScore(incident_id="INC-1", helpful=2, not_helpful=2)
    assert _score_rows(session_factory) == [("INC-1", 2, 2)]


def test_the_score_row_is_stamped_with_the_clock_time(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-1", MERCHANT)
    _insert_score(session_factory, MERCHANT, "INC-1", helpful=0, not_helpful=0)

    # Act
    _record(session_factory, clock, "incident", "INC-1", "helpful")

    # Assert
    with session_factory() as session:
        updated_at = session.execute(
            text("SELECT updated_at FROM ops_ai.incident_feedback_scores WHERE incident_id = 'INC-1'")
        ).scalar_one()
    assert updated_at == TEST_NOW


def test_another_merchants_score_for_the_same_incident_is_not_touched(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-PLATFORM", None)
    _insert_score(session_factory, OTHER_MERCHANT, "INC-PLATFORM", helpful=5, not_helpful=0)

    # Act
    result = _record(session_factory, clock, "incident", "INC-PLATFORM", "helpful")

    # Assert
    assert result.incident_score == IncidentScore(incident_id="INC-PLATFORM", helpful=1, not_helpful=0)
    assert _score_rows(session_factory, OTHER_MERCHANT) == [("INC-PLATFORM", 5, 0)]


def test_a_platform_incident_with_a_null_merchant_is_accepted(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-PLATFORM", None)

    # Act
    result = _record(session_factory, clock, "incident", "INC-PLATFORM", "helpful")

    # Assert
    assert (result.target_kind, result.target_id) == ("incident", "INC-PLATFORM")
    assert result.incident_score == IncidentScore(incident_id="INC-PLATFORM", helpful=1, not_helpful=0)


def test_an_incident_of_the_scope_merchant_is_accepted(
    session_factory: sessionmaker[Session], clock: FixedClock
) -> None:
    # Arrange
    _insert_incident(session_factory, "INC-OWN", MERCHANT)

    # Act
    result = _record(session_factory, clock, "incident", "INC-OWN", "not_helpful")

    # Assert
    assert result.incident_score == IncidentScore(incident_id="INC-OWN", helpful=0, not_helpful=1)


def test_answer_feedback_is_stored_only(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange / Act
    result = _record(session_factory, clock, "answer", "corr-1", "not_helpful", "wrong period")

    # Assert
    assert result.rule_version is None
    assert result.incident_score is None
    assert _feedback_rows(session_factory) == [
        (result.feedback_id, "answer", "corr-1", "not_helpful", "wrong period", TEST_NOW)
    ]
    assert _history(session_factory) == []
    assert _score_rows(session_factory) == []


# --- incident_scores ------------------------------------------------------------------------------------


def test_incident_scores_returns_only_the_ids_that_have_a_row(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_score(session_factory, MERCHANT, "INC-1", helpful=2, not_helpful=1)
    _insert_score(session_factory, MERCHANT, "INC-3", helpful=0, not_helpful=4)

    # Act
    with session_factory() as session:
        scores = incident_scores(session, SCOPE, ["INC-1", "INC-2", "INC-3"])

    # Assert
    assert scores == {
        "INC-1": IncidentScore(incident_id="INC-1", helpful=2, not_helpful=1),
        "INC-3": IncidentScore(incident_id="INC-3", helpful=0, not_helpful=4),
    }


def test_incident_scores_of_an_empty_id_list_is_empty(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _insert_score(session_factory, MERCHANT, "INC-1", helpful=2, not_helpful=1)

    # Act
    with session_factory() as session:
        scores = incident_scores(session, SCOPE, [])

    # Assert
    assert scores == {}


def test_incident_scores_never_returns_another_merchants_counts(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_score(session_factory, OTHER_MERCHANT, "INC-1", helpful=9, not_helpful=9)
    _insert_score(session_factory, MERCHANT, "INC-1", helpful=1, not_helpful=0)

    # Act
    with session_factory() as session:
        own = incident_scores(session, SCOPE, ["INC-1"])
        others = incident_scores(session, OTHER_SCOPE, ["INC-1"])

    # Assert
    assert own == {"INC-1": IncidentScore(incident_id="INC-1", helpful=1, not_helpful=0)}
    assert others == {"INC-1": IncidentScore(incident_id="INC-1", helpful=9, not_helpful=9)}


def test_record_feedback_does_not_commit(session_factory: sessionmaker[Session], clock: FixedClock) -> None:
    # Arrange
    alert_id = str(_insert_alert(session_factory, MERCHANT))

    # Act
    with session_factory() as session:
        result = record_feedback(session, SCOPE, clock, "alert", alert_id, "false_positive", None)
        session.rollback()

    # Assert
    assert result.rule_version is not None
    assert result.rule_version.version == 1
    assert _feedback_rows(session_factory) == []
    assert _history(session_factory) == []
