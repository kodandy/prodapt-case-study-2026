"""Tests for the dispute rubric in ops_ai/judge, written from docs/specs/evaluation-judge.md (6.1)."""

import copy
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest
from ops_ai.judge.dispute import REASON_CODES, JudgeReason, judge_dispute, rubric_checks
from ops_ai.judge.schemas import (
    DisputeEvidence,
    DisputeTransition,
    DisputeWorkflowRecord,
)
from pydantic import BaseModel, ValidationError

JUDGE_DATA_DIR = Path(__file__).resolve().parents[4] / "eval" / "judge"

OPENED_AT = "2026-10-01T09:00:00Z"
REVIEW_AT = "2026-10-02T10:00:00Z"
RESOLVED_AT = "2026-10-10T12:00:00Z"

CHECK_NAMES = [
    "transition_sequence",
    "transition_actors",
    "transition_order",
    "outcome_matches_state",
    "evidence_considered",
    "evidence_timing",
    "dispute_amount",
    "chargeback_amounts",
    "reason_code_known",
    "outcome_supported_by_evidence",
]
SEVERITIES = {
    "transition_sequence": "hard",
    "transition_actors": "hard",
    "transition_order": "hard",
    "outcome_matches_state": "hard",
    "evidence_considered": "hard",
    "evidence_timing": "hard",
    "dispute_amount": "hard",
    "chargeback_amounts": "hard",
    "reason_code_known": "soft",
    "outcome_supported_by_evidence": "soft",
}
RUBRIC_VERDICT_BY_SEVERITY = {"hard": "invalid", "soft": "needs_review"}


def tr(from_state: str | None, to_state: str, actor: str, at: str) -> dict[str, object]:
    return {"from_state": from_state, "to_state": to_state, "actor_role": actor, "at": at}


def ev(evidence_id: str, kind: str, at: str) -> dict[str, object]:
    return {"evidence_id": evidence_id, "kind": kind, "submitted_at": at}


def path(
    final: str | None,
    *,
    open_actor: str = "card_network",
    review_actor: str = "merchant",
    final_actor: str = "card_network",
    review_at: str = REVIEW_AT,
    final_at: str = RESOLVED_AT,
) -> list[dict[str, object]]:
    transitions = [
        tr(None, "opened", open_actor, OPENED_AT),
        tr("opened", "under_review", review_actor, review_at),
    ]
    if final is not None:
        transitions.append(tr("under_review", final, final_actor, final_at))
    return transitions


def won_data() -> dict[str, object]:
    return {
        "dispute_id": "00000000-0000-4000-8000-000000000101",
        "payment_id": "00000000-0000-4000-8000-000000000201",
        "merchant_id": "00000000-0000-4000-8000-000000000301",
        "network_reason_code": "13.1",
        "currency": "USD",
        "payment_amount_minor": 12000,
        "refunded_minor": 0,
        "dispute_amount_minor": 12000,
        "transitions": path("won"),
        "evidence": [ev("ev_1", "proof_of_delivery", REVIEW_AT)],
        "evidence_considered": ["ev_1"],
        "outcome": "won",
        "chargeback_minor": 0,
        "chargeback_fee_minor": 0,
    }


def lost_data() -> dict[str, object]:
    return {
        "dispute_id": "00000000-0000-4000-8000-000000000102",
        "payment_id": "00000000-0000-4000-8000-000000000202",
        "merchant_id": "00000000-0000-4000-8000-000000000301",
        "network_reason_code": "10.4",
        "currency": "USD",
        "payment_amount_minor": 5000,
        "refunded_minor": 0,
        "dispute_amount_minor": 5000,
        "transitions": path("lost", final_actor="ops_lead"),
        "evidence": [ev("ev_2", "customer_email", REVIEW_AT)],
        "evidence_considered": ["ev_2"],
        "outcome": "lost",
        "chargeback_minor": 5000,
        "chargeback_fee_minor": 1500,
    }


def make_record(base: str = "won", **overrides: object) -> DisputeWorkflowRecord:
    data = won_data() if base == "won" else lost_data()
    data.update(copy.deepcopy(overrides))
    return DisputeWorkflowRecord.model_validate(data)


def failed_names(record: DisputeWorkflowRecord) -> list[str]:
    return [check.check for check in rubric_checks(record) if not check.passed]


@dataclass(frozen=True)
class DisputeCase:
    case_id: str
    expected_verdict: str
    record: DisputeWorkflowRecord


def load_dispute_cases() -> list[DisputeCase]:
    payload = json.loads((JUDGE_DATA_DIR / "disputes.json").read_text(encoding="utf-8"))
    return [
        DisputeCase(
            case_id=case["case_id"],
            expected_verdict=case["expected_verdict"],
            record=DisputeWorkflowRecord.model_validate(case["record"]),
        )
        for case in payload["cases"]
    ]


DISPUTE_CASES = load_dispute_cases()

ISOLATED_FAILURES = [
    pytest.param(
        "won",
        {
            "transitions": [
                tr(None, "opened", "card_network", OPENED_AT),
                tr("opened", "under_review", "merchant", REVIEW_AT),
                tr("opened", "under_review", "merchant", "2026-10-03T10:00:00Z"),
            ],
            "outcome": None,
            "evidence_considered": [],
        },
        "transition_sequence",
        id="sequence-from-state-breaks-chain",
    ),
    pytest.param(
        "won",
        {"transitions": path("won", final_actor="merchant")},
        "transition_actors",
        id="actors-merchant-resolves",
    ),
    pytest.param(
        "won",
        {"transitions": path("won", open_actor="ops_lead")},
        "transition_actors",
        id="actors-ops-lead-opens",
    ),
    pytest.param(
        "won",
        {"transitions": path("won", review_actor="ops_lead")},
        "transition_actors",
        id="actors-ops-lead-starts-review",
    ),
    pytest.param(
        "lost",
        {"transitions": path("lost", final_actor="merchant")},
        "transition_actors",
        id="actors-merchant-resolves-lost",
    ),
    pytest.param(
        "won",
        {"transitions": path("won", review_at="2026-09-30T09:00:00Z")},
        "transition_order",
        id="order-review-before-open",
    ),
    pytest.param(
        "won",
        {"outcome": None},
        "outcome_matches_state",
        id="outcome-missing-for-resolved-state",
    ),
    pytest.param(
        "won",
        {"transitions": path(None), "outcome": "won"},
        "outcome_matches_state",
        id="outcome-set-while-unresolved",
    ),
    pytest.param(
        "won",
        {
            "evidence": [
                ev("ev_1", "proof_of_delivery", REVIEW_AT),
                ev("ev_2", "customer_email", REVIEW_AT),
            ],
            "evidence_considered": ["ev_1"],
        },
        "evidence_considered",
        id="considered-misses-submitted-id-when-resolved",
    ),
    pytest.param(
        "won",
        {"evidence_considered": ["ev_1", "ev_ghost"]},
        "evidence_considered",
        id="considered-has-unknown-id-when-resolved",
    ),
    pytest.param(
        "won",
        {"transitions": path(None), "outcome": None, "evidence_considered": ["ev_ghost"]},
        "evidence_considered",
        id="considered-has-unknown-id-when-unresolved",
    ),
    pytest.param(
        "won",
        {"evidence": [ev("ev_1", "proof_of_delivery", "2026-09-30T09:00:00Z")]},
        "evidence_timing",
        id="evidence-submitted-before-open",
    ),
    pytest.param(
        "won",
        {"evidence": [ev("ev_1", "proof_of_delivery", "2026-10-11T09:00:00Z")]},
        "evidence_timing",
        id="evidence-submitted-after-resolution",
    ),
    pytest.param(
        "won",
        {"refunded_minor": 2000},
        "dispute_amount",
        id="dispute-exceeds-payment-minus-refunded",
    ),
    pytest.param(
        "won",
        {"refunded_minor": 12000, "dispute_amount_minor": 1},
        "dispute_amount",
        id="dispute-on-fully-refunded-payment",
    ),
    pytest.param(
        "won",
        {"dispute_amount_minor": 0},
        "dispute_amount",
        id="dispute-amount-zero",
    ),
    pytest.param(
        "won",
        {"dispute_amount_minor": -1},
        "dispute_amount",
        id="dispute-amount-negative",
    ),
    pytest.param(
        "lost",
        {"chargeback_minor": 0},
        "chargeback_amounts",
        id="lost-without-chargeback",
    ),
    pytest.param(
        "lost",
        {"chargeback_minor": 4999},
        "chargeback_amounts",
        id="lost-chargeback-below-dispute-amount",
    ),
    pytest.param(
        "lost",
        {"chargeback_minor": 5001},
        "chargeback_amounts",
        id="lost-chargeback-above-dispute-amount",
    ),
    pytest.param(
        "lost",
        {"chargeback_fee_minor": 0},
        "chargeback_amounts",
        id="lost-with-zero-fee",
    ),
    pytest.param(
        "won",
        {"chargeback_minor": 100},
        "chargeback_amounts",
        id="won-with-chargeback",
    ),
    pytest.param(
        "won",
        {"chargeback_fee_minor": 100},
        "chargeback_amounts",
        id="won-with-chargeback-fee",
    ),
    pytest.param(
        "won",
        {"transitions": path(None), "outcome": None, "evidence_considered": [], "chargeback_minor": 100},
        "chargeback_amounts",
        id="unresolved-with-chargeback",
    ),
    pytest.param(
        "won",
        {"network_reason_code": "99.9", "evidence": [ev("ev_1", "customer_email", REVIEW_AT)]},
        "reason_code_known",
        id="reason-code-unknown",
    ),
    pytest.param(
        "won",
        {"network_reason_code": "", "evidence": [ev("ev_1", "customer_email", REVIEW_AT)]},
        "reason_code_known",
        id="reason-code-empty",
    ),
    pytest.param(
        "won",
        {"evidence": [ev("ev_1", "customer_email", REVIEW_AT)]},
        "outcome_supported_by_evidence",
        id="won-without-compelling-kind",
    ),
    pytest.param(
        "won",
        {"evidence": [ev("ev_1", "refund_issued", REVIEW_AT)]},
        "outcome_supported_by_evidence",
        id="won-with-kind-compelling-for-another-code",
    ),
    pytest.param(
        "won",
        {"evidence": [], "evidence_considered": []},
        "outcome_supported_by_evidence",
        id="won-without-any-evidence",
    ),
    pytest.param(
        "lost",
        {"evidence": [ev("ev_2", "3ds_authentication", REVIEW_AT)]},
        "outcome_supported_by_evidence",
        id="lost-with-compelling-kind-considered",
    ),
    pytest.param(
        "lost",
        {
            "evidence": [
                ev("ev_2", "customer_email", REVIEW_AT),
                ev("ev_3", "device_ip_match", REVIEW_AT),
            ],
            "evidence_considered": ["ev_2", "ev_3"],
        },
        "outcome_supported_by_evidence",
        id="lost-with-one-compelling-kind-among-several",
    ),
]

SEQUENCE_BREAKS = [
    pytest.param(
        [tr(None, "opened", "card_network", OPENED_AT), tr("opened", "won", "card_network", RESOLVED_AT)],
        id="opened-to-won-skips-review",
    ),
    pytest.param(
        [
            tr(None, "under_review", "merchant", OPENED_AT),
            tr("under_review", "won", "card_network", RESOLVED_AT),
        ],
        id="first-transition-is-not-opened",
    ),
    pytest.param(
        [
            tr("opened", "opened", "card_network", OPENED_AT),
            tr("opened", "under_review", "merchant", REVIEW_AT),
            tr("under_review", "won", "card_network", RESOLVED_AT),
        ],
        id="first-transition-has-a-from-state",
    ),
]

PASSING_VARIANTS = [
    pytest.param("won", {}, id="baseline-won"),
    pytest.param("lost", {}, id="baseline-lost"),
    pytest.param(
        "won",
        {
            "transitions": path("won", review_at=OPENED_AT, final_at=OPENED_AT),
            "evidence": [ev("ev_1", "proof_of_delivery", OPENED_AT)],
        },
        id="equal-timestamps-are-non-decreasing",
    ),
    pytest.param(
        "won",
        {
            "evidence": [
                ev("ev_1", "proof_of_delivery", OPENED_AT),
                ev("ev_2", "customer_email", RESOLVED_AT),
            ],
            "evidence_considered": ["ev_1", "ev_2"],
        },
        id="evidence-at-open-and-resolution-instants",
    ),
    pytest.param(
        "won",
        {
            "transitions": path(None),
            "outcome": None,
            "evidence": [ev("ev_1", "proof_of_delivery", "2027-01-01T00:00:00Z")],
            "evidence_considered": ["ev_1"],
        },
        id="unresolved-evidence-has-no-upper-bound",
    ),
    pytest.param(
        "won",
        {"transitions": path(None), "outcome": None, "evidence_considered": []},
        id="unresolved-with-nothing-considered",
    ),
    pytest.param(
        "won",
        {"refunded_minor": 2000, "dispute_amount_minor": 10000},
        id="dispute-equals-refundable",
    ),
    pytest.param(
        "won",
        {"evidence_considered": ["ev_1", "ev_1"]},
        id="duplicate-considered-ids-compare-as-a-set",
    ),
    pytest.param(
        "won",
        {
            "evidence": [
                ev("ev_1", "customer_email", REVIEW_AT),
                ev("ev_2", "proof_of_delivery", REVIEW_AT),
            ],
            "evidence_considered": ["ev_1", "ev_2"],
        },
        id="won-needs-only-one-compelling-kind",
    ),
    pytest.param(
        "won",
        {"transitions": path(None), "outcome": None, "evidence": [ev("ev_1", "customer_email", REVIEW_AT)]},
        id="unresolved-needs-no-evidence-support",
    ),
]


class TestRubricChecks:
    def test_returns_the_ten_checks_in_spec_order(self) -> None:
        # Arrange
        record = make_record()

        # Act
        checks = rubric_checks(record)

        # Assert
        assert type(checks) is tuple
        assert [check.check for check in checks] == CHECK_NAMES

    def test_check_severities_match_the_spec(self) -> None:
        # Arrange
        record = make_record()

        # Act
        checks = rubric_checks(record)

        # Assert
        assert {check.check: check.severity for check in checks} == SEVERITIES

    def test_baseline_won_record_passes_every_check_and_is_valid(self) -> None:
        # Arrange
        record = make_record()

        # Act
        checks = rubric_checks(record)
        verdict = judge_dispute(record)

        # Assert
        assert [check.passed for check in checks] == [True] * 10
        assert verdict.rubric_verdict == "valid"
        assert verdict.verdict == "valid"

    @pytest.mark.parametrize(("base", "overrides", "failing"), ISOLATED_FAILURES)
    def test_check_fails_in_isolation_with_its_severity(
        self, base: str, overrides: dict[str, object], failing: str
    ) -> None:
        # Arrange
        record = make_record(base, **overrides)

        # Act
        checks = rubric_checks(record)
        verdict = judge_dispute(record)

        # Assert
        failed = [check for check in checks if not check.passed]
        assert [check.check for check in failed] == [failing]
        assert failed[0].severity == SEVERITIES[failing]
        assert failed[0].reason != ""
        assert verdict.rubric_verdict == RUBRIC_VERDICT_BY_SEVERITY[SEVERITIES[failing]]

    @pytest.mark.parametrize("transitions", SEQUENCE_BREAKS)
    def test_illegal_transition_sequences_fail_transition_sequence_and_are_invalid(
        self, transitions: list[dict[str, object]]
    ) -> None:
        # Arrange
        record = make_record("won", transitions=transitions)

        # Act
        names = failed_names(record)
        verdict = judge_dispute(record)

        # Assert
        assert "transition_sequence" in names
        assert verdict.rubric_verdict == "invalid"

    @pytest.mark.parametrize(("base", "overrides"), PASSING_VARIANTS)
    def test_legal_variants_pass_every_check(self, base: str, overrides: dict[str, object]) -> None:
        # Arrange
        record = make_record(base, **overrides)

        # Act
        names = failed_names(record)
        verdict = judge_dispute(record)

        # Assert
        assert names == []
        assert verdict.rubric_verdict == "valid"

    def test_hard_and_soft_failures_together_are_invalid_and_both_reported(self) -> None:
        # Arrange
        record = make_record("won", refunded_minor=2000, network_reason_code="99.9")

        # Act
        verdict = judge_dispute(record)

        # Assert
        assert failed_names(record) == ["dispute_amount", "reason_code_known"]
        assert verdict.rubric_verdict == "invalid"
        assert verdict.verdict == "invalid"
        assert [reason.code for reason in verdict.reasons] == ["dispute_amount", "reason_code_known"]

    def test_soft_failures_only_are_needs_review(self) -> None:
        # Arrange
        record = make_record("won", evidence=[ev("ev_1", "customer_email", REVIEW_AT)])

        # Act
        verdict = judge_dispute(record)

        # Assert
        assert verdict.rubric_verdict == "needs_review"
        assert verdict.verdict == "needs_review"

    def test_rubric_is_deterministic_for_equal_records(self) -> None:
        # Arrange
        first = make_record("lost", chargeback_minor=0)
        second = make_record("lost", chargeback_minor=0)

        # Act
        first_checks = rubric_checks(first)
        second_checks = rubric_checks(second)

        # Assert
        assert first_checks == second_checks


class TestReasonCodes:
    def test_catalog_has_exactly_the_six_spec_codes(self) -> None:
        # Arrange
        expected = {"10.4", "12.6.1", "13.1", "13.2", "13.3", "13.6"}

        # Act
        codes = set(REASON_CODES)

        # Assert
        assert codes == expected

    @pytest.mark.parametrize(
        ("code", "compelling"),
        [
            ("10.4", {"avs_cvv_match", "3ds_authentication", "prior_undisputed_history", "device_ip_match"}),
            ("12.6.1", {"distinct_transactions_proof", "refund_issued"}),
            ("13.1", {"proof_of_delivery", "tracking_confirmation"}),
            ("13.2", {"cancellation_policy_accepted", "usage_after_cancellation"}),
            ("13.3", {"product_description", "return_policy_accepted"}),
            ("13.6", {"refund_issued", "return_policy_accepted"}),
        ],
    )
    def test_compelling_evidence_kinds_match_the_spec(self, code: str, compelling: set[str]) -> None:
        # Arrange
        reason_code = REASON_CODES[code]

        # Act
        kinds = reason_code.compelling_evidence

        # Assert
        assert kinds == frozenset(compelling)

    @pytest.mark.parametrize("code", ["10.4", "12.6.1", "13.1", "13.2", "13.3", "13.6"])
    def test_each_code_has_a_non_empty_category(self, code: str) -> None:
        # Arrange
        reason_code = REASON_CODES[code]

        # Act
        category = reason_code.category

        # Assert
        assert category != ""


class TestJudgeReason:
    @pytest.mark.parametrize("code", [*CHECK_NAMES, "llm_concern"])
    def test_accepts_every_check_name_and_llm_concern(self, code: str) -> None:
        # Arrange
        detail = "d"

        # Act
        reason = JudgeReason(code=code, detail=detail)

        # Assert
        assert reason.code == code
        assert reason.detail == detail

    def test_accepts_detail_of_300_characters(self) -> None:
        # Arrange
        detail = "x" * 300

        # Act
        reason = JudgeReason(code="llm_concern", detail=detail)

        # Assert
        assert reason.detail == detail

    def test_rejects_detail_of_301_characters(self) -> None:
        # Arrange
        detail = "x" * 301

        # Act / Assert
        with pytest.raises(ValidationError):
            JudgeReason(code="llm_concern", detail=detail)

    def test_rejects_unknown_code(self) -> None:
        # Arrange
        code = "made_up_check"

        # Act / Assert
        with pytest.raises(ValidationError):
            JudgeReason(code=code, detail="d")


class TestSchemas:
    def test_valid_dict_builds_a_record_with_typed_fields(self) -> None:
        # Arrange
        data = won_data()

        # Act
        record = DisputeWorkflowRecord.model_validate(data)

        # Assert
        assert record.dispute_id == uuid.UUID("00000000-0000-4000-8000-000000000101")
        assert record.network_reason_code == "13.1"
        assert len(record.transitions) == 3
        assert record.transitions[0].from_state is None
        assert record.transitions[2].to_state == "won"
        assert record.evidence[0].kind == "proof_of_delivery"
        assert record.evidence_considered == ("ev_1",)

    def test_record_rejects_extra_field(self) -> None:
        # Arrange
        data = won_data()
        data["surprise"] = 1

        # Act / Assert
        with pytest.raises(ValidationError):
            DisputeWorkflowRecord.model_validate(data)

    def test_record_rejects_empty_transitions(self) -> None:
        # Arrange
        data = won_data()
        data["transitions"] = []

        # Act / Assert
        with pytest.raises(ValidationError):
            DisputeWorkflowRecord.model_validate(data)

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("payment_amount_minor", 0),
            ("payment_amount_minor", -1),
            ("refunded_minor", -1),
            ("chargeback_minor", -1),
            ("chargeback_fee_minor", -1),
            ("outcome", "refunded"),
            ("dispute_id", "not-a-uuid"),
        ],
    )
    def test_record_rejects_out_of_range_values(self, field: str, value: object) -> None:
        # Arrange
        data = won_data()
        data[field] = value

        # Act / Assert
        with pytest.raises(ValidationError):
            DisputeWorkflowRecord.model_validate(data)

    @pytest.mark.parametrize("missing", ["dispute_id", "transitions", "outcome", "evidence_considered"])
    def test_record_rejects_missing_required_field(self, missing: str) -> None:
        # Arrange
        data = won_data()
        del data[missing]

        # Act / Assert
        with pytest.raises(ValidationError):
            DisputeWorkflowRecord.model_validate(data)

    @pytest.mark.parametrize(
        "overrides",
        [
            {"to_state": "reopened"},
            {"from_state": "closed"},
            {"actor_role": "customer"},
            {"at": "yesterday"},
            {"extra": True},
        ],
    )
    def test_transition_rejects_invalid_values(self, overrides: dict[str, object]) -> None:
        # Arrange
        data = tr(None, "opened", "card_network", OPENED_AT)
        data.update(overrides)

        # Act / Assert
        with pytest.raises(ValidationError):
            DisputeTransition.model_validate(data)

    def test_evidence_rejects_extra_field(self) -> None:
        # Arrange
        data = ev("ev_1", "proof_of_delivery", REVIEW_AT)
        data["extra"] = True

        # Act / Assert
        with pytest.raises(ValidationError):
            DisputeEvidence.model_validate(data)

    @pytest.mark.parametrize("model", [DisputeTransition, DisputeEvidence, DisputeWorkflowRecord])
    def test_dispute_models_are_frozen_and_forbid_extras(self, model: type[BaseModel]) -> None:
        # Arrange
        config = model.model_config

        # Act
        frozen = config.get("frozen")
        extra = config.get("extra")

        # Assert
        assert frozen is True
        assert extra == "forbid"


class TestGoldenDisputes:
    @pytest.mark.parametrize("case", [pytest.param(c, id=c.case_id) for c in DISPUTE_CASES])
    def test_rubric_only_verdict_matches_expected(self, case: DisputeCase) -> None:
        # Arrange
        record = case.record

        # Act
        verdict = judge_dispute(record)

        # Assert
        assert verdict.verdict == case.expected_verdict
        assert verdict.rubric_verdict == case.expected_verdict
        assert verdict.llm_status == "skipped"
        assert verdict.model is None
        assert verdict.dispute_id == record.dispute_id

    @pytest.mark.parametrize("case", [pytest.param(c, id=c.case_id) for c in DISPUTE_CASES])
    def test_one_reason_per_failed_check_in_check_order(self, case: DisputeCase) -> None:
        # Arrange
        record = case.record

        # Act
        verdict = judge_dispute(record)

        # Assert
        assert verdict.checks == rubric_checks(record)
        assert [reason.code for reason in verdict.reasons] == [
            check.check for check in verdict.checks if not check.passed
        ]
        assert [len(reason.detail) <= 300 for reason in verdict.reasons] == [True] * len(verdict.reasons)

    def test_golden_file_has_nine_cases_covering_all_three_verdicts(self) -> None:
        # Arrange
        expected = {"valid", "needs_review", "invalid"}

        # Act
        verdicts = {case.expected_verdict for case in DISPUTE_CASES}

        # Assert
        assert len(DISPUTE_CASES) == 9
        assert verdicts == expected
