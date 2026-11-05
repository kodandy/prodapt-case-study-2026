"""Acting principals and append-only rule audits (docs/specs/r2-agents.md section 6.1)."""

import dataclasses
import uuid
from datetime import datetime, timedelta

import pytest
from ops_ai.clock import Clock
from ops_ai.feedback.service import (
    FeedbackRejectedError,
    IncidentScore,
    Verdict,
    record_feedback,
)
from ops_ai.feedback.thresholds import (
    BASELINE,
    RuleAuditEntry,
    RuleVersion,
    active_rule_version,
    actor_of,
    append_rule_version,
    revert_rule,
    rule_audit,
    rule_history,
    thresholds_for_steps,
)
from ops_ai.scope import MerchantRole, Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from conftest import TEST_NOW

MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
OTHER_SCOPE = Scope(merchant_id=OTHER_MERCHANT)

INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, "
    "started_at, last_seen_at, payment_count, features, created_at) VALUES "
    "(:alert_id, :window_key, :merchant_id, 'card_testing', :first_payment_id, "
    ":created_at, :created_at, 12, '{}'::jsonb, :created_at)"
)
INSERT_VERSION = text(
    "INSERT INTO ops_ai.fraud_rule_versions (merchant_id, version, steps, min_micro_payments, "
    "min_micro_share, min_distinct_cards, reason, feedback_id, created_at) VALUES "
    "(:merchant_id, :version, :steps, :payments, :share, :cards, :reason, :feedback_id, :created_at)"
)
INSERT_ACTOR = text(
    "INSERT INTO ops_ai.fraud_rule_version_actors "
    "(merchant_id, version, actor, actor_role, created_at) VALUES "
    "(:merchant_id, :version, :actor, :actor_role, :created_at)"
)


def _insert_alert(session_factory: sessionmaker[Session], merchant_id: uuid.UUID = MERCHANT) -> uuid.UUID:
    alert_id = uuid.uuid4()
    with session_factory.begin() as session:
        session.execute(
            INSERT_ALERT,
            {
                "alert_id": alert_id,
                "window_key": f"card_testing:{merchant_id}:{alert_id}",
                "merchant_id": merchant_id,
                "first_payment_id": uuid.uuid4(),
                "created_at": TEST_NOW - timedelta(hours=1),
            },
        )
    return alert_id


def _insert_version(
    session_factory: sessionmaker[Session],
    merchant_id: uuid.UUID,
    version: int,
    steps: int,
    *,
    role: MerchantRole | None = None,
    created_at: datetime = TEST_NOW,
    reason: str = "feedback:false_positive",
    feedback_id: uuid.UUID | None = None,
) -> None:
    payments, share, cards = thresholds_for_steps(steps)
    with session_factory.begin() as session:
        session.execute(
            INSERT_VERSION,
            {
                "merchant_id": merchant_id,
                "version": version,
                "steps": steps,
                "payments": payments,
                "share": share,
                "cards": cards,
                "reason": reason,
                "feedback_id": feedback_id,
                "created_at": created_at,
            },
        )
        if role is not None:
            session.execute(
                INSERT_ACTOR,
                {
                    "merchant_id": merchant_id,
                    "version": version,
                    "actor": f"merchant:{merchant_id}",
                    "actor_role": role,
                    "created_at": created_at + timedelta(minutes=5),
                },
            )


def _actors(session: Session) -> list[tuple[uuid.UUID, int, str, str, datetime]]:
    rows = session.execute(
        text(
            "SELECT merchant_id, version, actor, actor_role, created_at "
            "FROM ops_ai.fraud_rule_version_actors ORDER BY merchant_id, version"
        )
    ).all()
    return [(row.merchant_id, row.version, row.actor, row.actor_role, row.created_at) for row in rows]


def _entry(
    version: int,
    reason: str,
    feedback_id: uuid.UUID | None = None,
    *,
    scope: Scope = SCOPE,
    created_at: datetime = TEST_NOW,
) -> RuleAuditEntry:
    return RuleAuditEntry(
        version=version,
        reason=reason,
        feedback_id=feedback_id,
        actor=f"merchant:{scope.merchant_id}",
        actor_role=scope.role,
        created_at=created_at,
    )


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
@pytest.mark.parametrize("merchant_id", [MERCHANT, OTHER_MERCHANT])
def test_actor_of_uses_the_merchant_token_subject_for_both_roles(
    role: MerchantRole, merchant_id: uuid.UUID
) -> None:
    # Arrange
    scope = Scope(merchant_id=merchant_id, role=role)

    # Act
    actor = actor_of(scope)

    # Assert
    assert actor == f"merchant:{merchant_id}"


def test_rule_audit_entry_is_frozen() -> None:
    # Arrange
    entry = _entry(1, "revert:v0")

    # Act
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.actor_role = "merchant_support"  # type: ignore[misc]

    # Assert
    assert entry == _entry(1, "revert:v0")


def test_rule_version_keeps_its_existing_fields_without_actor_fields() -> None:
    # Arrange
    rule = BASELINE

    # Act
    field_names = tuple(field.name for field in dataclasses.fields(rule))

    # Assert
    assert field_names == (
        "version",
        "steps",
        "min_micro_payments",
        "min_micro_share",
        "min_distinct_cards",
        "reason",
        "feedback_id",
        "created_at",
    )
    assert dataclasses.asdict(rule) == {
        "version": 0,
        "steps": 0,
        "min_micro_payments": 10,
        "min_micro_share": 0.8,
        "min_distinct_cards": 3,
        "reason": "baseline",
        "feedback_id": None,
        "created_at": None,
    }


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
def test_append_rule_version_writes_one_actor_with_the_unchanged_rule_version(
    session_factory: sessionmaker[Session], clock: Clock, role: MerchantRole
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT, role=role)
    feedback_id = uuid.uuid4()

    # Act
    with session_factory.begin() as session:
        result = append_rule_version(
            session, scope, clock, 2, (14, 0.86, 5), "feedback:false_positive", feedback_id
        )

    # Assert
    assert result == RuleVersion(1, 2, 14, 0.86, 5, "feedback:false_positive", feedback_id, TEST_NOW)
    with session_factory() as session:
        assert _actors(session) == [(MERCHANT, 1, f"merchant:{MERCHANT}", role, TEST_NOW)]
        assert rule_audit(session, scope) == [_entry(1, result.reason, feedback_id, scope=scope)]
        assert active_rule_version(session, scope) == result
        assert rule_history(session, scope) == [result]


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
@pytest.mark.parametrize(
    ("starting_steps", "verdict", "expected_steps"),
    [(0, "false_positive", 1), (2, "true_positive", 1)],
)
def test_feedback_that_moves_steps_records_the_acting_scope_and_feedback_link(
    session_factory: sessionmaker[Session],
    clock: Clock,
    role: MerchantRole,
    starting_steps: int,
    verdict: Verdict,
    expected_steps: int,
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT, role=role)
    alert_id = _insert_alert(session_factory)
    if starting_steps > 0:
        _insert_version(session_factory, MERCHANT, 1, starting_steps)
    version = 1 if starting_steps == 0 else 2

    # Act
    with session_factory.begin() as session:
        result = record_feedback(session, scope, clock, "alert", str(alert_id), verdict, "operator review")

    # Assert
    payments, share, cards = thresholds_for_steps(expected_steps)
    assert result.rule_version == RuleVersion(
        version, expected_steps, payments, share, cards, f"feedback:{verdict}", result.feedback_id, TEST_NOW
    )
    with session_factory() as session:
        assert _actors(session) == [(MERCHANT, version, f"merchant:{MERCHANT}", role, TEST_NOW)]
        assert rule_audit(session, scope)[0] == _entry(
            version, f"feedback:{verdict}", result.feedback_id, scope=scope
        )
        assert session.execute(
            text("SELECT feedback_id, merchant_id, verdict FROM ops_ai.feedback")
        ).one() == (result.feedback_id, MERCHANT, verdict)


@pytest.mark.parametrize(("starting_steps", "verdict"), [(0, "true_positive"), (5, "false_positive")])
def test_feedback_at_a_clamp_writes_feedback_without_a_version_or_actor(
    session_factory: sessionmaker[Session], clock: Clock, starting_steps: int, verdict: Verdict
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)
    if starting_steps > 0:
        _insert_version(session_factory, MERCHANT, 1, starting_steps)
    with session_factory() as session:
        history_before = rule_history(session, SCOPE)
        audit_before = rule_audit(session, SCOPE)

    # Act
    with session_factory.begin() as session:
        result = record_feedback(session, SCOPE, clock, "alert", str(alert_id), verdict, None)

    # Assert
    assert (result.rule_version, result.incident_score) == (None, None)
    with session_factory() as session:
        assert _actors(session) == []
        assert rule_history(session, SCOPE) == history_before
        assert rule_audit(session, SCOPE) == audit_before
        assert session.execute(text("SELECT feedback_id FROM ops_ai.feedback")).scalars().all() == [
            result.feedback_id
        ]


def test_answer_feedback_writes_no_rule_actor(session_factory: sessionmaker[Session], clock: Clock) -> None:
    # Arrange
    target_id = "opaque-answer-id"

    # Act
    with session_factory.begin() as session:
        result = record_feedback(session, SCOPE, clock, "answer", target_id, "helpful", None)

    # Assert
    assert (result.rule_version, result.incident_score) == (None, None)
    with session_factory() as session:
        assert _actors(session) == []
        assert rule_audit(session, SCOPE) == []
        assert rule_history(session, SCOPE) == []
        assert session.execute(text("SELECT target_id FROM ops_ai.feedback")).scalars().all() == [target_id]


@pytest.mark.parametrize(("verdict", "helpful", "not_helpful"), [("helpful", 1, 0), ("not_helpful", 0, 1)])
def test_incident_feedback_updates_its_score_without_a_rule_version_or_actor(
    session_factory: sessionmaker[Session],
    clock: Clock,
    verdict: Verdict,
    helpful: int,
    not_helpful: int,
) -> None:
    # Arrange
    incident_id = "INC-RULE-ACTOR"
    with session_factory.begin() as session:
        session.execute(
            text(
                "INSERT INTO ops_ai.incidents (incident_id, merchant_id, title, root_cause, "
                "fix_applied, resolution_notes, started_at, resolved_at, recurrence_within_7d) "
                "VALUES (:incident_id, :merchant_id, 'Issuer outage', 'Issuer timeouts', "
                "'Rerouted', 'notes', :created_at, :created_at, false)"
            ),
            {"incident_id": incident_id, "merchant_id": MERCHANT, "created_at": TEST_NOW},
        )

    # Act
    with session_factory.begin() as session:
        result = record_feedback(session, SCOPE, clock, "incident", incident_id, verdict, None)

    # Assert
    assert (result.rule_version, result.incident_score) == (
        None,
        IncidentScore(incident_id, helpful, not_helpful),
    )
    with session_factory() as session:
        assert _actors(session) == []
        assert rule_audit(session, SCOPE) == []
        assert rule_history(session, SCOPE) == []
        assert session.execute(text("SELECT target_id, verdict FROM ops_ai.feedback")).all() == [
            (incident_id, verdict)
        ]
        assert session.execute(
            text(
                "SELECT merchant_id, incident_id, helpful, not_helpful, updated_at "
                "FROM ops_ai.incident_feedback_scores"
            )
        ).all() == [(MERCHANT, incident_id, helpful, not_helpful, TEST_NOW)]


@pytest.mark.parametrize("role", ["merchant_admin", "merchant_support"])
@pytest.mark.parametrize("target_version", [0, 1, 2])
def test_revert_records_a_new_actor_for_baseline_prior_and_current_versions(
    session_factory: sessionmaker[Session], clock: Clock, role: MerchantRole, target_version: int
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT, role=role)
    _insert_version(session_factory, MERCHANT, 1, 1)
    _insert_version(session_factory, MERCHANT, 2, 2)
    steps = target_version
    payments, share, cards = thresholds_for_steps(steps)

    # Act
    with session_factory.begin() as session:
        result = revert_rule(session, scope, clock, target_version)

    # Assert
    assert result == RuleVersion(
        3, steps, payments, share, cards, f"revert:v{target_version}", None, TEST_NOW
    )
    with session_factory() as session:
        assert _actors(session) == [(MERCHANT, 3, f"merchant:{MERCHANT}", role, TEST_NOW)]
        assert rule_audit(session, scope)[0] == _entry(3, f"revert:v{target_version}", scope=scope)
        assert [row.version for row in rule_history(session, scope)] == [3, 2, 1]


def test_revert_to_baseline_without_history_creates_version_one_and_actor(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    scope = Scope(merchant_id=MERCHANT, role="merchant_support")

    # Act
    with session_factory.begin() as session:
        result = revert_rule(session, scope, clock, 0)

    # Assert
    assert result == dataclasses.replace(BASELINE, version=1, reason="revert:v0", created_at=TEST_NOW)
    with session_factory() as session:
        assert _actors(session) == [(MERCHANT, 1, f"merchant:{MERCHANT}", "merchant_support", TEST_NOW)]
        assert rule_audit(session, scope) == [_entry(1, "revert:v0", scope=scope)]


def test_unknown_revert_including_another_merchants_version_writes_no_actor(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, OTHER_MERCHANT, 7, 3, role="merchant_support")
    with session_factory() as session:
        actors_before = _actors(session)
        other_history = rule_history(session, OTHER_SCOPE)

    # Act
    with session_factory.begin() as session:
        results = (revert_rule(session, SCOPE, clock, 7), revert_rule(session, SCOPE, clock, 99))

    # Assert
    assert results == (None, None)
    with session_factory() as session:
        assert _actors(session) == actors_before
        assert rule_audit(session, SCOPE) == []
        assert rule_history(session, SCOPE) == []
        assert rule_history(session, OTHER_SCOPE) == other_history


def test_append_rolls_back_the_version_and_actor_together(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    scope = SCOPE

    # Act
    with session_factory() as session:
        result = append_rule_version(session, scope, clock, 1, (12, 0.83, 4), "manual", None)
        pending_actors = _actors(session)
        pending_audit = rule_audit(session, scope)
        session.rollback()

    # Assert
    assert result == RuleVersion(1, 1, 12, 0.83, 4, "manual", None, TEST_NOW)
    assert pending_actors == [(MERCHANT, 1, f"merchant:{MERCHANT}", "merchant_admin", TEST_NOW)]
    assert pending_audit == [_entry(1, "manual")]
    with session_factory() as session:
        assert _actors(session) == []
        assert rule_audit(session, scope) == []
        assert rule_history(session, scope) == []
        assert active_rule_version(session, scope) == BASELINE


def test_feedback_rolls_back_feedback_version_and_actor_together(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)

    # Act
    with session_factory() as session:
        result = record_feedback(session, SCOPE, clock, "alert", str(alert_id), "false_positive", None)
        pending_audit = rule_audit(session, SCOPE)
        pending_actors = _actors(session)
        session.rollback()

    # Assert
    assert pending_audit == [_entry(1, "feedback:false_positive", result.feedback_id)]
    assert pending_actors == [(MERCHANT, 1, f"merchant:{MERCHANT}", "merchant_admin", TEST_NOW)]
    with session_factory() as session:
        assert _actors(session) == []
        assert rule_audit(session, SCOPE) == []
        assert rule_history(session, SCOPE) == []
        assert session.execute(text("SELECT count(*) FROM ops_ai.feedback")).scalar_one() == 0


def test_revert_rolls_back_only_its_new_version_and_actor(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    _insert_version(session_factory, MERCHANT, 1, 2, role="merchant_support")
    with session_factory() as session:
        actors_before = _actors(session)
        history_before = rule_history(session, SCOPE)
        audit_before = rule_audit(session, SCOPE)

    # Act
    with session_factory() as session:
        result = revert_rule(session, SCOPE, clock, 0)
        pending_audit = rule_audit(session, SCOPE)
        pending_actors = _actors(session)
        session.rollback()

    # Assert
    assert result == RuleVersion(2, 0, 10, 0.8, 3, "revert:v0", None, TEST_NOW)
    assert pending_audit == [_entry(2, "revert:v0"), *audit_before]
    assert pending_actors == [
        *actors_before,
        (MERCHANT, 2, f"merchant:{MERCHANT}", "merchant_admin", TEST_NOW),
    ]
    with session_factory() as session:
        assert _actors(session) == actors_before
        assert rule_history(session, SCOPE) == history_before
        assert rule_audit(session, SCOPE) == audit_before


def test_rule_audit_without_versions_has_no_baseline_entry(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert_version(session_factory, OTHER_MERCHANT, 1, 1, role="merchant_admin")

    # Act
    with session_factory() as session:
        entries = rule_audit(session, SCOPE)

    # Assert
    assert entries == []


def test_rule_audit_left_joins_both_keys_and_orders_by_version_not_timestamp(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    old_time = TEST_NOW - timedelta(days=2)
    new_time = TEST_NOW - timedelta(days=1)
    feedback_id = uuid.uuid4()
    _insert_version(session_factory, MERCHANT, 1, 1, created_at=TEST_NOW)
    _insert_version(
        session_factory, MERCHANT, 3, 0, role="merchant_support", created_at=old_time, reason="revert:v0"
    )
    _insert_version(
        session_factory,
        MERCHANT,
        2,
        2,
        role="merchant_admin",
        created_at=new_time,
        feedback_id=feedback_id,
    )
    for version in (1, 2, 3, 4):
        _insert_version(session_factory, OTHER_MERCHANT, version, 5, role="merchant_support")

    # Act
    with session_factory() as session:
        entries = rule_audit(session, SCOPE)
        other_entries = rule_audit(session, OTHER_SCOPE)

    # Assert
    assert entries == [
        _entry(3, "revert:v0", scope=Scope(MERCHANT, "merchant_support"), created_at=old_time),
        _entry(2, "feedback:false_positive", feedback_id, created_at=new_time),
        RuleAuditEntry(1, "feedback:false_positive", None, None, None, TEST_NOW),
    ]
    assert other_entries == [
        _entry(version, "feedback:false_positive", scope=Scope(OTHER_MERCHANT, "merchant_support"))
        for version in (4, 3, 2, 1)
    ]


def test_feedback_for_each_merchant_records_only_that_merchants_principal(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    own_alert = _insert_alert(session_factory)
    other_alert = _insert_alert(session_factory, OTHER_MERCHANT)
    support_scope = Scope(OTHER_MERCHANT, "merchant_support")

    # Act
    with session_factory.begin() as session:
        own = record_feedback(session, SCOPE, clock, "alert", str(own_alert), "false_positive", None)
        other = record_feedback(
            session, support_scope, clock, "alert", str(other_alert), "false_positive", None
        )

    # Assert
    with session_factory() as session:
        assert _actors(session) == [
            (MERCHANT, 1, f"merchant:{MERCHANT}", "merchant_admin", TEST_NOW),
            (OTHER_MERCHANT, 1, f"merchant:{OTHER_MERCHANT}", "merchant_support", TEST_NOW),
        ]
        assert rule_audit(session, SCOPE) == [_entry(1, "feedback:false_positive", own.feedback_id)]
        assert rule_audit(session, support_scope) == [
            _entry(1, "feedback:false_positive", other.feedback_id, scope=support_scope)
        ]


def test_rejected_duplicate_feedback_does_not_append_a_second_actor(
    session_factory: sessionmaker[Session], clock: Clock
) -> None:
    # Arrange
    alert_id = _insert_alert(session_factory)
    with session_factory.begin() as session:
        result = record_feedback(session, SCOPE, clock, "alert", str(alert_id), "false_positive", None)

    # Act
    with pytest.raises(FeedbackRejectedError) as raised, session_factory.begin() as session:
        record_feedback(session, SCOPE, clock, "alert", str(alert_id), "false_positive", None)

    # Assert
    assert raised.value.reason == "duplicate"
    with session_factory() as session:
        assert _actors(session) == [(MERCHANT, 1, f"merchant:{MERCHANT}", "merchant_admin", TEST_NOW)]
        assert rule_audit(session, SCOPE) == [_entry(1, "feedback:false_positive", result.feedback_id)]
