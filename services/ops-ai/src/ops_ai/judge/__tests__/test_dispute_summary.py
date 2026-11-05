"""RED tests for dispute agent judgement, evaluation-judge.md section 6.4.1."""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from ops_ai.judge.dispute import judge_dispute
from ops_ai.judge.dispute_summary import (
    MAX_JUDGED_DISPUTES,
    DisputeJudgement,
    judge_merchant_disputes,
)
from ops_ai.judge.schemas import DisputeWorkflowRecord
from ops_ai.scope import Scope

MERCHANT = uuid.UUID(int=301)
SCOPE = Scope(MERCHANT)
OPENED_AT = "2026-10-01T09:00:00Z"
REVIEW_AT = "2026-10-02T10:00:00Z"
RESOLVED_AT = "2026-10-10T12:00:00Z"


def tr(from_state: str | None, to_state: str, actor: str, at: str) -> dict[str, object]:
    return {"from_state": from_state, "to_state": to_state, "actor_role": actor, "at": at}


def ev(evidence_id: str, kind: str, at: str) -> dict[str, object]:
    return {"evidence_id": evidence_id, "kind": kind, "submitted_at": at}


def make_record(number: int = 101, **overrides: object) -> DisputeWorkflowRecord:
    data: dict[str, object] = {
        "dispute_id": uuid.UUID(int=number),
        "payment_id": uuid.UUID(int=201),
        "merchant_id": MERCHANT,
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 12000,
        "refunded_minor": 0,
        "dispute_amount_minor": 12000,
        "transitions": [
            tr(None, "opened", "card_network", OPENED_AT),
            tr("opened", "under_review", "merchant", REVIEW_AT),
            tr("under_review", "won", "card_network", RESOLVED_AT),
        ],
        "evidence": [ev("ev_1", "proof_of_delivery", REVIEW_AT)],
        "evidence_considered": ["ev_1"],
        "outcome": "won",
        "chargeback_minor": 0,
        "chargeback_fee_minor": 0,
    }
    data.update(overrides)
    return DisputeWorkflowRecord.model_validate(data)


@dataclass
class StubDisputeRecordSource:
    records: Sequence[DisputeWorkflowRecord] = ()
    error: Exception | None = None
    calls: list[tuple[Scope, int]] = field(default_factory=list)

    def dispute_records(self, scope: Scope, limit: int) -> Sequence[DisputeWorkflowRecord]:
        self.calls.append((scope, limit))
        if self.error is not None:
            raise self.error
        return self.records


def skipped(reason: str) -> DisputeJudgement:
    return DisputeJudgement(
        status="skipped",
        reason=reason,
        disputes=0,
        verdict=None,
        score=None,
        passed=None,
        rationale=None,
        dispute_ids=(),
    )


def test_no_source_skips_without_fabricating_records() -> None:
    # Arrange
    source = None
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert result == skipped("no dispute workflow source is configured")


@pytest.mark.parametrize("error", [RuntimeError("private detail"), ValueError("bad records")])
def test_source_exception_is_reported_as_unavailable(error: Exception) -> None:
    # Arrange
    source = StubDisputeRecordSource(error=error)
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert result == skipped("dispute workflow records unavailable")
    assert source.calls == [(SCOPE, MAX_JUDGED_DISPUTES)]


@pytest.mark.parametrize("records", [(), (make_record(merchant_id=uuid.UUID(int=999)),)])
def test_empty_or_all_foreign_records_skip_with_exact_reason(
    records: tuple[DisputeWorkflowRecord, ...],
) -> None:
    # Arrange
    source = StubDisputeRecordSource(records)
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert result == skipped("no dispute workflow record for this merchant")


def test_foreign_records_are_dropped_before_limiting_and_source_order_is_preserved() -> None:
    # Arrange
    foreign = make_record(merchant_id=uuid.UUID(int=999), dispute_amount_minor=0)
    local = tuple(make_record(number) for number in range(150, 125, -1))
    source = StubDisputeRecordSource((foreign,) * MAX_JUDGED_DISPUTES + local)
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert MAX_JUDGED_DISPUTES == 20
    assert source.calls == [(SCOPE, 20)]
    assert result == DisputeJudgement(
        status="judged",
        reason=None,
        disputes=20,
        verdict="valid",
        score=1.0,
        passed=True,
        rationale="20 dispute workflow(s) pass the rubric",
        dispute_ids=tuple(record.dispute_id for record in local[:20]),
    )


def test_all_valid_records_pass_with_exact_rationale_and_ids_in_source_order() -> None:
    # Arrange
    records = (make_record(103), make_record(101))
    source = StubDisputeRecordSource(records)
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert result == DisputeJudgement(
        status="judged",
        reason=None,
        disputes=2,
        verdict="valid",
        score=1.0,
        passed=True,
        rationale="2 dispute workflow(s) pass the rubric",
        dispute_ids=(records[0].dispute_id, records[1].dispute_id),
    )


def test_invalid_record_sets_worst_verdict_and_mean_score_with_bounded_rationale() -> None:
    # Arrange
    invalid = make_record(102, dispute_amount_minor=12001)
    records = (make_record(101), invalid)
    source = StubDisputeRecordSource(records)
    per_record = tuple(judge_dispute(record) for record in records)
    expected_score = round(
        sum(sum(check.passed for check in item.checks) / len(item.checks) for item in per_record) / 2, 4
    )
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert (result.status, result.reason, result.disputes) == ("judged", None, 2)
    assert (result.verdict, result.passed, result.score) == ("invalid", False, expected_score)
    assert result.score == 0.95
    assert result.dispute_ids == (records[0].dispute_id, invalid.dispute_id)
    expected = f"dispute {invalid.dispute_id}: " + "; ".join(r.detail for r in per_record[1].reasons)
    assert result.rationale == expected[:300]
    assert isinstance(result.rationale, str)
    assert result.rationale.startswith(f"dispute {invalid.dispute_id}: ")
    assert len(result.rationale) <= 300


def test_soft_failure_needs_review_and_uses_first_nonvalid_record_reasons() -> None:
    # Arrange
    review = make_record(102, network_reason_code="unknown")
    invalid = make_record(103, dispute_amount_minor=0)
    source = StubDisputeRecordSource((make_record(), review, invalid))
    expected = f"dispute {review.dispute_id}: reason code 'unknown' is not in the catalog"
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    review_only = judge_merchant_disputes(SCOPE, StubDisputeRecordSource((review,)))
    # Assert
    assert (review_only.verdict, review_only.passed, review_only.score) == ("needs_review", False, 0.9)
    assert review_only.rationale == expected
    assert (result.verdict, result.passed, result.score) == ("invalid", False, 0.9333)
    assert result.rationale == expected


def test_failure_rationale_is_truncated_to_300_characters() -> None:
    # Arrange
    record = make_record(evidence=[ev("e" * 400, "proof_of_delivery", REVIEW_AT)])
    verdict = judge_dispute(record)
    expected = f"dispute {record.dispute_id}: " + "; ".join(reason.detail for reason in verdict.reasons)
    source = StubDisputeRecordSource((record,))
    # Act
    result = judge_merchant_disputes(SCOPE, source)
    # Assert
    assert result.rationale == expected[:300]
    assert isinstance(result.rationale, str)
    assert len(result.rationale) == 300
    assert (result.verdict, result.passed) == ("invalid", False)
