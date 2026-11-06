"""One investigate request uses one pooled DB connection: the dispute record source reads through the
request's own session instead of checking out a second one (security audit M-1)."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from contracts.events import DisputeEventV1
from ops_ai.agents.messages import JsonScalar
from ops_ai.agents.supervisor import AgentInvestigation, run_agents
from ops_ai.db import make_session_factory
from ops_ai.ingest.disputes import apply_dispute_event
from ops_ai.judge import dispute_summary
from ops_ai.judge.dispute_records import PostgresDisputeRecordSource
from ops_ai.scope import Scope
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
OPENED_AT = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
QUESTION = "Any dispute open?"
POOL_TIMEOUT_SECONDS = 1


def _project_lost_dispute(session_factory: sessionmaker[Session], merchant_id: uuid.UUID) -> uuid.UUID:
    dispute_id = uuid.uuid4()
    shared = {
        "dispute_id": dispute_id,
        "payment_id": uuid.uuid4(),
        "merchant_id": merchant_id,
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 1500,
        "refunded_minor": 0,
        "dispute_amount_minor": 1500,
    }
    steps = [
        ("dispute.opened", None, "opened", "card_network", 0, 0),
        ("dispute.evidence_submitted", "opened", "under_review", "merchant", 0, 0),
        ("dispute.resolved", "under_review", "lost", "ops_lead", 1500, 1500),
    ]
    for index, (event_type, from_state, to_state, actor_role, chargeback, fee) in enumerate(steps):
        event = DisputeEventV1.model_validate(
            {
                **shared,
                "event_id": uuid.uuid4(),
                "event_type": event_type,
                "occurred_at": OPENED_AT + timedelta(hours=index),
                "entity_version": index + 1,
                "from_state": from_state,
                "to_state": to_state,
                "actor_role": actor_role,
                "chargeback_minor": chargeback,
                "chargeback_fee_minor": fee,
            }
        )
        with session_factory.begin() as session:
            apply_dispute_event(session, lambda: OPENED_AT, event)
    return dispute_id


@pytest.fixture
def single_connection_factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    small = create_engine(
        engine.url.render_as_string(hide_password=False),
        pool_size=1,
        max_overflow=0,
        pool_timeout=POOL_TIMEOUT_SECONDS,
    )
    try:
        yield make_session_factory(small)
    finally:
        small.dispose()


class _NoSessionFactory(sessionmaker[Session]):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def __call__(self, **local_kw: object) -> Session:
        self.calls += 1
        raise AssertionError("a bound dispute record source must not open its own session")


def _finding_payload(result: AgentInvestigation) -> dict[str, JsonScalar]:
    payloads = [
        dict(message.payload)
        for message in result.messages
        if message.sender == "dispute" and message.recipient == "supervisor" and message.intent == "finding"
    ]
    assert len(payloads) == 1
    return payloads[0]


def test_the_investigation_judges_disputes_with_a_single_pooled_connection(
    session_factory: sessionmaker[Session], single_connection_factory: sessionmaker[Session]
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    dispute_id = _project_lost_dispute(session_factory, merchant_id)
    source = PostgresDisputeRecordSource(single_connection_factory)

    # Act
    with single_connection_factory.begin() as session:
        session.execute(text("SELECT 1"))
        result = run_agents(session, Scope(merchant_id=merchant_id), QUESTION, NOW, dispute_records=source)

    # Assert
    payload = _finding_payload(result)
    assert (payload["judge_status"], payload["judge_reason"], payload["judge_disputes"]) == (
        "judged",
        None,
        1,
    )
    assert payload["judge_verdict"] == "valid"
    dispute = next(c for c in result.contributions if c.agent == "dispute")
    assert dispute.judge is not None
    assert dispute.judge.dispute_ids == (dispute_id,)


def test_the_postgres_source_can_be_bound_to_a_request_session() -> None:
    # Arrange
    source = PostgresDisputeRecordSource(_NoSessionFactory())

    # Act
    bindable = isinstance(source, dispute_summary.SessionBindableDisputeRecordSource)

    # Assert
    assert bindable is True


def test_a_bound_source_reads_through_the_given_session_and_never_opens_its_own(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    merchant_id = uuid.uuid4()
    dispute_id = _project_lost_dispute(session_factory, merchant_id)
    _project_lost_dispute(session_factory, uuid.uuid4())
    no_sessions = _NoSessionFactory()
    source = PostgresDisputeRecordSource(no_sessions)

    # Act
    with session_factory() as session:
        records = source.bound(session).dispute_records(
            Scope(merchant_id=merchant_id), dispute_summary.MAX_JUDGED_DISPUTES
        )

    # Assert
    assert [(record.dispute_id, record.merchant_id, record.outcome) for record in records] == [
        (dispute_id, merchant_id, "lost")
    ]
    assert no_sessions.calls == 0
