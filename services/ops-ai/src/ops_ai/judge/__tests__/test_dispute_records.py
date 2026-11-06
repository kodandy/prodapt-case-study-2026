"""Tests for ops_ai.judge.dispute_records: the Postgres DisputeRecordSource over gw.disputes.v1."""

import uuid
from datetime import UTC, datetime, timedelta

from contracts.events import DisputeEventV1
from ops_ai.ingest.disputes import apply_dispute_event
from ops_ai.judge.dispute import judge_dispute
from ops_ai.judge.dispute_records import (
    GATEWAY_EVIDENCE_KIND,
    UNSPECIFIED_REASON_CODE,
    PostgresDisputeRecordSource,
)
from ops_ai.judge.dispute_summary import judge_merchant_disputes
from ops_ai.scope import Scope
from sqlalchemy.orm import Session, sessionmaker

OPENED_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)


def _now() -> datetime:
    return datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _events(
    dispute_id: uuid.UUID,
    *,
    merchant_id: uuid.UUID = MERCHANT,
    outcome: str = "lost",
    steps: int = 3,
    start: datetime = OPENED_AT,
) -> list[DisputeEventV1]:
    shared = {
        "dispute_id": dispute_id,
        "payment_id": uuid.UUID(int=dispute_id.int ^ 1),
        "merchant_id": merchant_id,
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 1500,
        "refunded_minor": 0,
        "dispute_amount_minor": 1200,
    }
    lost = outcome == "lost"
    rows = [
        ("dispute.opened", None, "opened", "card_network", 0, 0),
        ("dispute.evidence_submitted", "opened", "under_review", "merchant", 0, 0),
        ("dispute.resolved", "under_review", outcome, "ops_lead", 1200 if lost else 0, 1500 if lost else 0),
    ]
    return [
        DisputeEventV1.model_validate(
            {
                **shared,
                "event_id": uuid.uuid4(),
                "event_type": event_type,
                "occurred_at": start + timedelta(hours=index),
                "entity_version": index + 1,
                "from_state": from_state,
                "to_state": to_state,
                "actor_role": actor_role,
                "chargeback_minor": chargeback,
                "chargeback_fee_minor": fee,
            }
        )
        for index, (event_type, from_state, to_state, actor_role, chargeback, fee) in enumerate(rows[:steps])
    ]


def _project(session_factory: sessionmaker[Session], events: list[DisputeEventV1]) -> None:
    for event in events:
        with session_factory.begin() as session:
            apply_dispute_event(session, _now, event)


def test_a_lost_workflow_becomes_a_record_the_rubric_rates_valid(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    dispute_id = uuid.uuid4()
    events = _events(dispute_id)
    _project(session_factory, events)
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    records = source.dispute_records(SCOPE, 20)

    # Assert
    assert len(records) == 1
    record = records[0]
    assert (record.dispute_id, record.merchant_id, record.payment_id) == (
        dispute_id,
        MERCHANT,
        events[0].payment_id,
    )
    assert [(t.from_state, t.to_state, t.actor_role, t.at) for t in record.transitions] == [
        (None, "opened", "card_network", events[0].occurred_at),
        ("opened", "under_review", "merchant", events[1].occurred_at),
        ("under_review", "lost", "ops_lead", events[2].occurred_at),
    ]
    assert [(e.evidence_id, e.kind, e.submitted_at) for e in record.evidence] == [
        (str(events[1].event_id), GATEWAY_EVIDENCE_KIND, events[1].occurred_at)
    ]
    assert record.evidence_considered == (str(events[1].event_id),)
    assert (record.outcome, record.chargeback_minor, record.chargeback_fee_minor) == ("lost", 1200, 1500)
    assert judge_dispute(record).verdict == "valid"


def test_an_open_dispute_has_no_outcome_and_no_considered_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _project(session_factory, _events(uuid.uuid4(), steps=2))
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    record = source.dispute_records(SCOPE, 20)[0]

    # Assert
    assert [t.to_state for t in record.transitions] == ["opened", "under_review"]
    assert (record.outcome, record.evidence_considered, len(record.evidence)) == (None, (), 1)
    assert judge_dispute(record).verdict == "valid"


def test_another_merchants_disputes_never_appear(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    own = uuid.uuid4()
    _project(session_factory, _events(own))
    _project(session_factory, _events(uuid.uuid4(), merchant_id=OTHER_MERCHANT))
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    records = source.dispute_records(SCOPE, 20)
    other = source.dispute_records(Scope(merchant_id=OTHER_MERCHANT), 20)

    # Assert
    assert [record.dispute_id for record in records] == [own]
    assert [record.merchant_id for record in other] == [OTHER_MERCHANT]


def test_a_merchant_without_disputes_gets_no_record(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    _project(session_factory, _events(uuid.uuid4(), merchant_id=OTHER_MERCHANT))
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    records = source.dispute_records(SCOPE, 20)

    # Assert
    assert list(records) == []


def test_a_workflow_with_a_step_not_yet_delivered_is_left_out(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    complete = uuid.uuid4()
    _project(session_factory, _events(complete))
    opened, _evidence, resolved = _events(uuid.uuid4())
    _project(session_factory, [opened, resolved])
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    records = source.dispute_records(SCOPE, 20)

    # Assert
    assert [record.dispute_id for record in records] == [complete]


def test_the_limit_keeps_the_most_recently_updated_disputes(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    older, newer, newest = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    for offset, dispute_id in enumerate((older, newer, newest)):
        _project(session_factory, _events(dispute_id, start=OPENED_AT + timedelta(days=offset)))
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    records = source.dispute_records(SCOPE, 2)

    # Assert
    assert [record.dispute_id for record in records] == [newest, newer]


def test_the_dispute_judge_rates_projected_workflows_instead_of_skipping(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    lost, won = uuid.uuid4(), uuid.uuid4()
    _project(session_factory, _events(lost))
    _project(session_factory, _events(won, outcome="won", start=OPENED_AT + timedelta(days=1)))
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    judgement = judge_merchant_disputes(SCOPE, source)

    # Assert
    assert (judgement.status, judgement.reason, judgement.disputes) == ("judged", None, 2)
    assert (judgement.verdict, judgement.passed) == ("needs_review", False)
    assert judgement.dispute_ids == (won, lost)
    assert judgement.rationale == f"dispute {won}: won without compelling evidence for reason code 13.1"


def test_a_consumer_opened_dispute_with_a_free_text_reason_needs_review_for_its_reason_code(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    opened = _events(uuid.uuid4(), steps=1)[0]
    consumer_opened = opened.model_copy(update={"actor_role": "consumer", "network_reason_code": None})
    _project(session_factory, [consumer_opened])
    source = PostgresDisputeRecordSource(session_factory)

    # Act
    record = source.dispute_records(SCOPE, 20)[0]
    verdict = judge_dispute(record)

    # Assert
    assert (record.network_reason_code, record.transitions[0].actor_role) == (
        UNSPECIFIED_REASON_CODE,
        "consumer",
    )
    failed = [check.check for check in verdict.checks if not check.passed]
    assert (verdict.verdict, failed) == ("needs_review", ["reason_code_known"])
