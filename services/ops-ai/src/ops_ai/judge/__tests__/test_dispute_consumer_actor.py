"""The consumer may only open a dispute; any other consumer or merchant-open step is invalid."""

import pytest
from ops_ai.judge.dispute import judge_dispute, rubric_checks
from ops_ai.judge.schemas import DisputeWorkflowRecord

OPENED_AT = "2026-10-01T09:00:00Z"
REVIEW_AT = "2026-10-02T10:00:00Z"
RESOLVED_AT = "2026-10-10T12:00:00Z"
_EVIDENCE_KIND = {"won": "proof_of_delivery", "lost": "customer_email"}


def _record(
    open_actor: str, review_actor: str, final_actor: str, outcome: str = "won"
) -> DisputeWorkflowRecord:
    lost = outcome == "lost"
    return DisputeWorkflowRecord.model_validate(
        {
            "dispute_id": "00000000-0000-4000-8000-000000000501",
            "payment_id": "00000000-0000-4000-8000-000000000601",
            "merchant_id": "00000000-0000-4000-8000-000000000701",
            "network_reason_code": "10.4" if lost else "13.1",
            "currency": "USD",
            "payment_amount_minor": 5000,
            "refunded_minor": 0,
            "dispute_amount_minor": 5000,
            "transitions": [
                {"from_state": None, "to_state": "opened", "actor_role": open_actor, "at": OPENED_AT},
                {
                    "from_state": "opened",
                    "to_state": "under_review",
                    "actor_role": review_actor,
                    "at": REVIEW_AT,
                },
                {
                    "from_state": "under_review",
                    "to_state": outcome,
                    "actor_role": final_actor,
                    "at": RESOLVED_AT,
                },
            ],
            "evidence": [{"evidence_id": "ev_1", "kind": _EVIDENCE_KIND[outcome], "submitted_at": REVIEW_AT}],
            "evidence_considered": ["ev_1"],
            "outcome": outcome,
            "chargeback_minor": 5000 if lost else 0,
            "chargeback_fee_minor": 1500 if lost else 0,
        }
    )


def _failed(record: DisputeWorkflowRecord) -> list[str]:
    return [check.check for check in rubric_checks(record) if not check.passed]


@pytest.mark.parametrize(
    ("open_actor", "review_actor", "final_actor", "outcome"),
    [
        pytest.param("consumer", "consumer", "card_network", "won", id="consumer-starts-review"),
        pytest.param("consumer", "merchant", "consumer", "won", id="consumer-resolves-won"),
        pytest.param("consumer", "merchant", "consumer", "lost", id="consumer-resolves-lost"),
        pytest.param("merchant", "merchant", "card_network", "won", id="merchant-opens"),
    ],
)
def test_a_step_by_an_actor_not_allowed_for_it_makes_the_workflow_invalid(
    open_actor: str, review_actor: str, final_actor: str, outcome: str
) -> None:
    # Arrange
    record = _record(open_actor, review_actor, final_actor, outcome)

    # Act
    verdict = judge_dispute(record)

    # Assert
    assert (verdict.rubric_verdict, verdict.verdict) == ("invalid", "invalid")
    assert _failed(record) == ["transition_actors"]


def test_a_consumer_open_followed_by_merchant_review_and_network_resolution_is_valid() -> None:
    # Arrange
    record = _record("consumer", "merchant", "card_network")

    # Act
    verdict = judge_dispute(record)

    # Assert
    assert (verdict.rubric_verdict, verdict.verdict) == ("valid", "valid")
    assert _failed(record) == []
