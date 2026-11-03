"""Tests for investigate_fraud, written from docs/specs/sq3.md section 5 (small hand-built data)."""

import dataclasses
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.fraud_watch.service import AlertRef, FraudInvestigation, investigate_fraud
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import Scope
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=24)
BURST_START = NOW - timedelta(hours=2)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SCOPE = Scope(merchant_id=MERCHANT)
QUESTION = "We're seeing a series of tiny card payments - is this card testing, and what should we do?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
FIXES = (
    "Add a velocity limit on card payments of 1.00 or less, per card and per client IP.",
    "Require 3-D Secure for card payments of 1.00 or less until the burst stops.",
    "Do not refund or contact the cards in the burst from this channel; the fraud team works the ticket.",
)
UNSUPPORTED = FraudInvestigation(
    intent="unsupported",
    cause_label="unsupported",
    analysis=None,
    alerts=(),
    suggested_fixes=(),
    tool_calls=(),
    evidence=(),
    mode="t2_fallback",
    text=None,
    model=None,
)

INSERT_FACT = text(
    "INSERT INTO ops_ai.payment_facts (payment_id, merchant_id, amount_minor, currency, method, state, "
    "provider, decline_code, card_bin, card_ref, entity_version, occurred_at) VALUES "
    "(:payment_id, :merchant_id, :amount_minor, 'USD', 'card', :state, 'acq_a', NULL, :card_bin, :card_ref, "
    "1, :occurred_at)"
)
INSERT_ALERT = text(
    "INSERT INTO ops_ai.fraud_alerts (alert_id, window_key, merchant_id, kind, first_payment_id, started_at, "
    "last_seen_at, payment_count, features, created_at) VALUES (:alert_id, :window_key, :merchant_id, "
    "'card_testing', :first_payment_id, :started_at, :last_seen_at, 12, '{}'::jsonb, :created_at)"
)
INSERT_TICKET = text(
    "INSERT INTO ops_ai.tickets (ticket_id, source_key, queue, priority, merchant_id, title, reason, "
    "alert_id, created_at) VALUES (:ticket_id, :source_key, :queue, 'high', :merchant_id, "
    "'Card testing suspected', '12 micro payments', :alert_id, :created_at)"
)


@dataclass
class RecordingLlm:
    text: str = NARRATION
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=self.text, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


def _card_ref(n: int) -> str:
    return f"{n:032x}"


def _burst(
    session_factory: sessionmaker[Session],
    *,
    count: int = 12,
    start: datetime = BURST_START,
    step_seconds: int = 10,
    first_n: int = 1,
    cards: int | None = 4,
    declined: int = 6,
    bins: Sequence[str] = ("400000",),
    merchant_id: uuid.UUID = MERCHANT,
    card_offset: int = 0,
    amount_minor: int = 50,
) -> list[uuid.UUID]:
    ids = [uuid.UUID(int=first_n + index) for index in range(count)]
    with session_factory.begin() as session:
        for index, payment_id in enumerate(ids):
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": payment_id,
                    "merchant_id": merchant_id,
                    "amount_minor": amount_minor,
                    "state": "declined" if index < declined else "succeeded",
                    "card_bin": bins[index % len(bins)],
                    "card_ref": None if cards is None else _card_ref(card_offset + index % cards + 1),
                    "occurred_at": start + timedelta(seconds=index * step_seconds),
                },
            )
    return ids


def _larger_payments(
    session_factory: sessionmaker[Session], placements: Sequence[tuple[int, datetime]]
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    with session_factory.begin() as session:
        for n, at in placements:
            ids.append(uuid.UUID(int=n))
            session.execute(
                INSERT_FACT,
                {
                    "payment_id": ids[-1],
                    "merchant_id": MERCHANT,
                    "amount_minor": 5000,
                    "state": "succeeded",
                    "card_bin": "999999",
                    "card_ref": _card_ref(900),
                    "occurred_at": at,
                },
            )
    return ids


def _alert(
    session_factory: sessionmaker[Session],
    first_payment_id: uuid.UUID,
    *,
    merchant_id: uuid.UUID = MERCHANT,
    queue: str | None = "fraud",
    started_at: datetime = BURST_START,
) -> tuple[uuid.UUID, uuid.UUID | None]:
    alert_id = uuid.uuid4()
    ticket_id = uuid.uuid4() if queue is not None else None
    window_key = f"card_testing:{merchant_id}:{first_payment_id}"
    with session_factory.begin() as session:
        session.execute(
            INSERT_ALERT,
            {
                "alert_id": alert_id,
                "window_key": window_key,
                "merchant_id": merchant_id,
                "first_payment_id": first_payment_id,
                "started_at": started_at,
                "last_seen_at": started_at + timedelta(seconds=110),
                "created_at": started_at,
            },
        )
        if ticket_id is not None:
            session.execute(
                INSERT_TICKET,
                {
                    "ticket_id": ticket_id,
                    "source_key": window_key,
                    "queue": queue,
                    "merchant_id": merchant_id,
                    "alert_id": alert_id,
                    "created_at": started_at,
                },
            )
    return alert_id, ticket_id


def _run(
    session_factory: sessionmaker[Session],
    llm: RecordingLlm | DownLlm | None = None,
    question: str = QUESTION,
    scope: Scope = SCOPE,
) -> FraudInvestigation:
    with session_factory() as session:
        return investigate_fraud(session, scope, question, NOW, llm or RecordingLlm())


def _refs(result: FraudInvestigation) -> list[str]:
    return [item.ref for item in result.evidence]


# --- intent -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "question",
    [
        QUESTION,
        "Is this card testing?",
        "Looks like Card-Testing to me",
        "CARD TESTING?",
        "Why so many micro charges?",
        "micro",
    ],
    ids=["sq3_question", "card_testing", "hyphenated", "upper_case", "micro_charges", "micro_alone"],
)
def test_investigate_fraud_treats_card_testing_card_dash_testing_or_micro_as_the_card_testing_intent(
    session_factory: sessionmaker[Session], question: str
) -> None:
    # Arrange: empty database, only the intent matters

    # Act
    result = _run(session_factory, question=question)

    # Assert
    assert result.intent == "card_testing"
    assert [call.name for call in result.tool_calls] == ["analyze_fraud"]


@pytest.mark.parametrize(
    "question",
    [
        "We're seeing a series of tiny card payments, what should we do?",
        "What is my settlement balance?",
        "15% of my card payments are failing",
        "Hello",
    ],
)
def test_investigate_fraud_marks_any_other_question_as_unsupported_without_tools_or_llm(
    session_factory: sessionmaker[Session], question: str
) -> None:
    # Arrange: a flagged burst exists, but the question does not ask about it
    _burst(session_factory)
    llm = RecordingLlm()

    # Act
    result = _run(session_factory, llm=llm, question=question)

    # Assert
    assert result == UNSUPPORTED
    assert llm.calls == []


# --- tool call and analysis -------------------------------------------------------------------------------


def test_investigate_fraud_records_one_analyze_fraud_call_over_the_24_hours_before_now(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.tool_calls == (
        ToolCall(name="analyze_fraud", args={"start": WINDOW_START.isoformat(), "end": NOW.isoformat()}),
    )


def test_investigate_fraud_carries_the_analysis_of_the_scope_merchant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.analysis is not None
    assert result.analysis.micro_payments == 12
    assert [episode.payment_ids for episode in result.analysis.episodes] == [tuple(ids)]


def test_investigate_fraud_includes_a_payment_exactly_24_hours_before_now_and_excludes_one_at_now(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: eleven payments from exactly 24 h before now; the twelfth sits exactly at now
    ids = _burst(session_factory, count=12, start=WINDOW_START, step_seconds=10)
    with session_factory.begin() as session:
        session.execute(
            text("UPDATE ops_ai.payment_facts SET occurred_at = :now WHERE payment_id = :id"),
            {"now": NOW, "id": ids[11]},
        )

    # Act
    result = _run(session_factory)

    # Assert
    assert result.analysis is not None
    assert [episode.payment_ids for episode in result.analysis.episodes] == [tuple(ids[:11])]
    assert result.analysis.episodes[0].start == WINDOW_START


# --- cause label ------------------------------------------------------------------------------------------


def test_investigate_fraud_labels_a_flagged_episode_as_card_testing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.cause_label == "card_testing"
    assert result.suggested_fixes == FIXES


def test_investigate_fraud_labels_an_empty_history_as_no_card_testing_without_fixes_or_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: nothing stored

    # Act
    result = _run(session_factory)

    # Assert
    assert result.intent == "card_testing"
    assert result.cause_label == "no_card_testing"
    assert result.suggested_fixes == ()
    assert result.evidence == ()
    assert result.alerts == ()
    assert result.analysis is not None
    assert result.analysis.episodes == ()


def test_investigate_fraud_labels_an_unflagged_single_card_episode_as_no_card_testing(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory, cards=1, declined=0)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.cause_label == "no_card_testing"
    assert result.suggested_fixes == ()
    assert result.evidence == ()
    assert result.analysis is not None
    assert [(e.payment_ids, e.flagged) for e in result.analysis.episodes] == [(tuple(ids), False)]


@pytest.mark.parametrize(
    "start",
    [NOW - timedelta(hours=25), NOW + timedelta(hours=1)],
    ids=["older_than_24_hours", "after_now"],
)
def test_investigate_fraud_ignores_a_burst_outside_the_24_hour_window(
    session_factory: sessionmaker[Session], start: datetime
) -> None:
    # Arrange
    _burst(session_factory, start=start)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.cause_label == "no_card_testing"
    assert result.evidence == ()


def test_investigate_fraud_never_shows_another_merchants_burst_or_alert(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    others = _burst(session_factory, first_n=1000, merchant_id=OTHER_MERCHANT)
    _alert(session_factory, others[0], merchant_id=OTHER_MERCHANT)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.cause_label == "no_card_testing"
    assert result.alerts == ()
    assert result.evidence == ()


# --- alerts -----------------------------------------------------------------------------------------------


def test_investigate_fraud_lists_the_alert_of_a_flagged_episode_with_its_ticket_and_queue(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    alert_id, ticket_id = _alert(session_factory, ids[0])

    # Act
    result = _run(session_factory)

    # Assert
    assert result.alerts == (
        AlertRef(alert_id=alert_id, ticket_id=ticket_id, queue="fraud", first_payment_id=ids[0]),
    )


def test_investigate_fraud_lists_an_alert_without_a_ticket_with_none_for_ticket_and_queue(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    alert_id, _ticket = _alert(session_factory, ids[0], queue=None)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.alerts == (
        AlertRef(alert_id=alert_id, ticket_id=None, queue=None, first_payment_id=ids[0]),
    )


def test_investigate_fraud_lists_only_alerts_that_start_at_the_first_payment_of_a_flagged_episode(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: a stale alert of the same merchant points at a payment that starts no episode
    ids = _burst(session_factory)
    own_alert, own_ticket = _alert(session_factory, ids[0])
    _alert(session_factory, ids[5])
    _alert(session_factory, uuid.UUID(int=424242))
    _alert(session_factory, ids[0], merchant_id=OTHER_MERCHANT)

    # Act
    result = _run(session_factory)

    # Assert
    assert result.alerts == (
        AlertRef(alert_id=own_alert, ticket_id=own_ticket, queue="fraud", first_payment_id=ids[0]),
    )


def test_investigate_fraud_lists_no_alert_for_an_unflagged_episode(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory, cards=1, declined=0)
    _alert(session_factory, ids[0])

    # Act
    result = _run(session_factory)

    # Assert
    assert result.alerts == ()


# --- evidence ---------------------------------------------------------------------------------------------


def test_investigate_fraud_evidence_is_the_episode_then_the_alert_then_the_five_newest_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    alert_id, _ticket = _alert(session_factory, ids[0])

    # Act
    result = _run(session_factory)

    # Assert
    assert _refs(result) == [
        f"episode:{ids[0]}",
        f"alert:{alert_id}",
        f"payment:{ids[11]}",
        f"payment:{ids[10]}",
        f"payment:{ids[9]}",
        f"payment:{ids[8]}",
        f"payment:{ids[7]}",
    ]


def test_investigate_fraud_evidence_without_an_alert_is_the_episode_and_the_payments(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory, count=12)

    # Act
    result = _run(session_factory)

    # Assert
    assert _refs(result) == [f"episode:{ids[0]}", *[f"payment:{ids[index]}" for index in (11, 10, 9, 8, 7)]]


def test_investigate_fraud_orders_two_flagged_episodes_newest_first_and_cites_payments_of_the_newest_only(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    older = _burst(session_factory, first_n=1, start=BURST_START)
    newer = _burst(session_factory, first_n=101, start=BURST_START + timedelta(minutes=10), card_offset=20)
    older_alert, _ = _alert(session_factory, older[0], started_at=BURST_START)
    newer_alert, _ = _alert(session_factory, newer[0], started_at=BURST_START + timedelta(minutes=10))

    # Act
    result = _run(session_factory)

    # Assert
    refs = _refs(result)
    assert refs[:2] == [f"episode:{newer[0]}", f"episode:{older[0]}"]
    assert set(refs[2:4]) == {f"alert:{newer_alert}", f"alert:{older_alert}"}
    assert refs[4:] == [f"payment:{newer[index]}" for index in (11, 10, 9, 8, 7)]
    assert len(result.alerts) == 2


def test_investigate_fraud_cites_payments_of_the_newest_flagged_episode_not_of_a_newer_unflagged_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    flagged = _burst(session_factory, first_n=1, start=BURST_START)
    unflagged = _burst(
        session_factory, first_n=101, start=BURST_START + timedelta(minutes=10), cards=1, declined=0
    )

    # Act
    result = _run(session_factory)

    # Assert
    refs = _refs(result)
    assert refs == [f"episode:{flagged[0]}", *[f"payment:{flagged[index]}" for index in (11, 10, 9, 8, 7)]]
    assert f"episode:{unflagged[0]}" not in refs


def test_investigate_fraud_episode_summary_names_count_span_cards_shares_and_bins(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange: 12 micro payments 30 s apart (a 5 minute 30 second span), 7 declined, 3 larger ones inside
    _burst(session_factory, count=12, step_seconds=30, cards=4, declined=7, bins=("424242", "400000"))
    _larger_payments(
        session_factory, [(500 + i, BURST_START + timedelta(seconds=15 + 30 * i)) for i in range(3)]
    )

    # Act
    result = _run(session_factory)

    # Assert
    summary = result.evidence[0].summary
    assert re.search(r"\b12\b", summary) is not None
    assert re.search(r"5\D+30", summary) is not None
    assert re.search(r"\b4\b", summary) is not None
    assert "80.0%" in summary
    assert "58.3%" in summary
    assert "400000" in summary
    assert "424242" in summary


def test_investigate_fraud_episode_summary_says_card_identity_unknown_for_unknown_cards(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _burst(session_factory, cards=None, declined=12)

    # Act
    result = _run(session_factory)

    # Assert
    summary = result.evidence[0].summary
    assert "card identity unknown" in summary
    assert "100.0%" in summary
    assert result.cause_label == "card_testing"


def test_investigate_fraud_alert_summary_names_the_ticket_and_the_queue(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    alert_id, ticket_id = _alert(session_factory, ids[0])

    # Act
    result = _run(session_factory)

    # Assert
    summary = next(item.summary for item in result.evidence if item.ref == f"alert:{alert_id}")
    assert str(ticket_id) in summary
    assert "fraud" in summary


def test_investigate_fraud_evidence_refs_are_unique(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    ids = _burst(session_factory)
    _alert(session_factory, ids[0])

    # Act
    refs = _refs(_run(session_factory))

    # Assert
    assert len(refs) == len(set(refs))


# --- llm --------------------------------------------------------------------------------------------------


def test_investigate_fraud_asks_the_llm_once_with_the_question_and_the_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    _alert(session_factory, ids[0])
    llm = RecordingLlm()

    # Act
    result = _run(session_factory, llm=llm)

    # Assert
    assert llm.calls == [LlmRequest(question=QUESTION, evidence=result.evidence)]
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)


def test_investigate_fraud_falls_back_to_t2_with_the_same_analysis_when_the_llm_is_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    ids = _burst(session_factory)
    _alert(session_factory, ids[0])
    with_llm = _run(session_factory, llm=RecordingLlm())

    # Act
    without_llm = _run(session_factory, llm=DownLlm())

    # Assert
    assert without_llm == dataclasses.replace(with_llm, mode="t2_fallback", text=None, model=None)
    assert without_llm.cause_label == "card_testing"
    assert len(without_llm.evidence) == 7


def test_investigate_fraud_label_and_ids_never_come_from_the_llm_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    llm = RecordingLlm(
        text="This is card_testing: payment:00000000-0000-0000-0000-000000000001 is the first."
    )

    # Act
    result = _run(session_factory, llm=llm)

    # Assert
    assert result.cause_label == "no_card_testing"
    assert result.evidence == ()
    assert result.suggested_fixes == ()
    assert result.text == llm.text
    assert len(llm.calls) == 1
    assert llm.calls[0].evidence == ()
