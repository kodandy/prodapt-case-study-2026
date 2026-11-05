"""Tests for the LLM leg of judge_dispute, written from docs/specs/evaluation-judge.md (6.1 and 6.3)."""

import copy
import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.judge.dispute import JudgeReason, judge_dispute, rubric_checks
from ops_ai.judge.fake import FakeJudgeLlm
from ops_ai.judge.schemas import DisputeWorkflowRecord

JUDGE_DATA_DIR = Path(__file__).resolve().parents[4] / "eval" / "judge"

VALID_CASE = "won_with_compelling_evidence"
NEEDS_REVIEW_CASE = "won_without_compelling_evidence"
INVALID_CASE = "skipped_review_state"
MAX_REPLY_BYTES = 8192


@dataclass(frozen=True)
class DisputeCase:
    case_id: str
    expected_verdict: str
    record: DisputeWorkflowRecord


def load_dispute_cases() -> dict[str, DisputeCase]:
    payload = json.loads((JUDGE_DATA_DIR / "disputes.json").read_text(encoding="utf-8"))
    return {
        case["case_id"]: DisputeCase(
            case_id=case["case_id"],
            expected_verdict=case["expected_verdict"],
            record=DisputeWorkflowRecord.model_validate(case["record"]),
        )
        for case in payload["cases"]
    }


CASES = load_dispute_cases()


class UnavailableLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmUnavailableError("provider down")


class ScriptedLlm:
    def __init__(self, text: str, model: str) -> None:
        self.text = text
        self.model = model

    def complete(self, request: LlmRequest) -> LlmResponse:
        return LlmResponse(text=self.text, model=self.model)


def reply_json(verdict: str, *details: str) -> str:
    reasons = [{"code": "llm_concern", "detail": detail} for detail in details]
    return json.dumps({"verdict": verdict, "reasons": reasons})


def failed_codes(record: DisputeWorkflowRecord) -> list[str]:
    return [check.check for check in rubric_checks(record) if not check.passed]


def padded_to(text: str, size: int) -> str:
    return text + " " * (size - len(text.encode("utf-8")))


NEEDS_REVIEW_REPLY = reply_json("needs_review", "odd")

REJECTED_REPLIES = [
    pytest.param("needs_review, I am fairly sure", id="not-json"),
    pytest.param("", id="empty-text"),
    pytest.param("[]", id="json-array-instead-of-object"),
    pytest.param(
        json.dumps({"verdict": "needs_review", "reasons": [], "confidence": 0.9}),
        id="extra-top-level-key",
    ),
    pytest.param(json.dumps({"reasons": []}), id="verdict-missing"),
    pytest.param(json.dumps({"verdict": "needs_review", "reasons": "odd"}), id="reasons-not-a-list"),
    pytest.param(reply_json("needs_review", *["odd"] * 11), id="eleven-reasons"),
    pytest.param(reply_json("needs_review", "x" * 301), id="detail-over-300-characters"),
    pytest.param(
        json.dumps(
            {"verdict": "needs_review", "reasons": [{"code": "llm_concern", "detail": "あ" * 300}] * 10},
            ensure_ascii=False,
        ),
        id="over-8-kib-in-utf8-bytes",
    ),
    pytest.param(padded_to(NEEDS_REVIEW_REPLY, MAX_REPLY_BYTES + 1), id="8193-bytes"),
    pytest.param(reply_json("approved"), id="verdict-outside-enum"),
    pytest.param(
        json.dumps({"verdict": "needs_review", "reasons": [{"code": "transition_sequence", "detail": "d"}]}),
        id="reason-code-is-a-rubric-check",
    ),
    pytest.param(
        json.dumps({"verdict": "needs_review", "reasons": [{"code": "something_else", "detail": "d"}]}),
        id="reason-code-unknown",
    ),
    pytest.param(
        json.dumps(
            {
                "verdict": "needs_review",
                "reasons": [{"code": "llm_concern", "detail": "d", "severity": "hard"}],
            }
        ),
        id="reason-item-extra-key",
    ),
    pytest.param(
        json.dumps({"verdict": "needs_review", "reasons": [{"code": "llm_concern"}]}),
        id="reason-item-detail-missing",
    ),
]


class TestFakeJudgeLlm:
    def test_new_fake_has_model_name_and_no_requests(self) -> None:
        # Arrange / Act
        fake = FakeJudgeLlm()

        # Assert
        assert fake.model == "fake-judge"
        assert fake.requests == []

    def test_scripted_reply_is_returned_verbatim_and_request_recorded(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply="exactly this")
        request = LlmRequest(question="q", evidence=())

        # Act
        response = fake.complete(request)

        # Assert
        assert response == LlmResponse(text="exactly this", model="fake-judge")
        assert fake.requests == [request]


class TestFakeJudgeAgreesWithRubric:
    @pytest.mark.parametrize("case", [pytest.param(c, id=c.case_id) for c in CASES.values()])
    def test_default_fake_reproduces_the_expected_verdict(self, case: DisputeCase) -> None:
        # Arrange
        fake = FakeJudgeLlm()

        # Act
        verdict = judge_dispute(case.record, fake)

        # Assert
        assert verdict.verdict == case.expected_verdict
        assert verdict.rubric_verdict == case.expected_verdict
        assert verdict.llm_status == "used"
        assert verdict.model == "fake-judge"

    @pytest.mark.parametrize("case", [pytest.param(c, id=c.case_id) for c in CASES.values()])
    def test_default_fake_receives_exactly_one_request_with_a_rubric_json_line(
        self, case: DisputeCase
    ) -> None:
        # Arrange
        fake = FakeJudgeLlm()

        # Act
        judge_dispute(case.record, fake)

        # Assert
        assert len(fake.requests) == 1
        lines = fake.requests[0].question.splitlines()
        assert len([line for line in lines if line.startswith("RUBRIC_JSON:")]) == 1


class TestVerdictCombination:
    @pytest.mark.parametrize(
        ("case_id", "llm_verdict", "expected"),
        [
            (VALID_CASE, "valid", "valid"),
            (VALID_CASE, "needs_review", "needs_review"),
            (VALID_CASE, "invalid", "needs_review"),
            (NEEDS_REVIEW_CASE, "valid", "needs_review"),
            (NEEDS_REVIEW_CASE, "needs_review", "needs_review"),
            (NEEDS_REVIEW_CASE, "invalid", "needs_review"),
            (INVALID_CASE, "valid", "invalid"),
            (INVALID_CASE, "needs_review", "invalid"),
            (INVALID_CASE, "invalid", "invalid"),
        ],
    )
    def test_llm_can_only_raise_valid_to_needs_review(
        self, case_id: str, llm_verdict: str, expected: str
    ) -> None:
        # Arrange
        record = CASES[case_id].record
        fake = FakeJudgeLlm(reply=reply_json(llm_verdict))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.verdict == expected
        assert verdict.rubric_verdict == CASES[case_id].expected_verdict
        assert verdict.llm_status == "used"

    def test_llm_never_produces_invalid_on_a_rubric_valid_record(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("invalid", "looks fabricated"))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.verdict == "needs_review"
        assert verdict.rubric_verdict == "valid"


class TestLlmReasons:
    def test_needs_review_reply_adds_a_trailing_llm_concern_reason(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("needs_review", "delivery proof predates the order"))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.verdict == "needs_review"
        assert verdict.reasons == (
            JudgeReason(code="llm_concern", detail="delivery proof predates the order"),
        )

    def test_llm_reasons_follow_rubric_reasons_in_reply_order(self) -> None:
        # Arrange
        record = CASES[NEEDS_REVIEW_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("needs_review", "first concern", "second concern"))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert [reason.code for reason in verdict.reasons] == [
            *failed_codes(record),
            "llm_concern",
            "llm_concern",
        ]
        assert [reason.detail for reason in verdict.reasons][-2:] == ["first concern", "second concern"]
        assert verdict.reasons[-1].code == "llm_concern"

    def test_llm_reasons_are_kept_when_the_verdict_is_not_raised(self) -> None:
        # Arrange
        record = CASES[INVALID_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("valid", "minor wording issue"))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.verdict == "invalid"
        assert [reason.code for reason in verdict.reasons] == [*failed_codes(record), "llm_concern"]
        assert verdict.reasons[-1].detail == "minor wording issue"

    def test_ten_reasons_are_accepted(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("needs_review", *[f"concern {i}" for i in range(10)]))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "used"
        assert [reason.detail for reason in verdict.reasons] == [f"concern {i}" for i in range(10)]

    def test_empty_reasons_list_is_accepted(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("valid"))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "used"
        assert verdict.reasons == ()
        assert verdict.verdict == "valid"

    def test_detail_of_300_characters_is_accepted(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply=reply_json("needs_review", "x" * 300))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "used"
        assert verdict.reasons == (JudgeReason(code="llm_concern", detail="x" * 300),)

    def test_fenced_json_reply_is_accepted(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply="```json\n" + reply_json("needs_review", "fenced") + "\n```")

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "used"
        assert verdict.verdict == "needs_review"
        assert verdict.reasons == (JudgeReason(code="llm_concern", detail="fenced"),)

    def test_reply_of_exactly_8_kib_is_accepted(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        reply = padded_to(NEEDS_REVIEW_REPLY, MAX_REPLY_BYTES)
        fake = FakeJudgeLlm(reply=reply)

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert len(reply.encode("utf-8")) == MAX_REPLY_BYTES
        assert verdict.llm_status == "used"
        assert verdict.verdict == "needs_review"


class TestRejectedReplies:
    @pytest.mark.parametrize("reply", REJECTED_REPLIES)
    def test_invalid_reply_is_dropped_and_the_rubric_verdict_stands(self, reply: str) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply=reply)

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "rejected"
        assert verdict.verdict == "valid"
        assert verdict.rubric_verdict == "valid"
        assert verdict.reasons == ()
        assert verdict.model == "fake-judge"

    @pytest.mark.parametrize("reply", REJECTED_REPLIES)
    def test_invalid_reply_adds_no_llm_concern_to_a_failing_rubric(self, reply: str) -> None:
        # Arrange
        record = CASES[NEEDS_REVIEW_CASE].record
        fake = FakeJudgeLlm(reply=reply)

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "rejected"
        assert verdict.verdict == "needs_review"
        assert [reason.code for reason in verdict.reasons] == failed_codes(record)

    def test_a_rejected_reply_is_still_one_llm_call(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        fake = FakeJudgeLlm(reply="not json")

        # Act
        judge_dispute(record, fake)

        # Assert
        assert len(fake.requests) == 1


class TestUnavailableLlm:
    @pytest.mark.parametrize("case_id", [VALID_CASE, NEEDS_REVIEW_CASE, INVALID_CASE])
    def test_outage_falls_back_to_the_rubric_verdict(self, case_id: str) -> None:
        # Arrange
        case = CASES[case_id]

        # Act
        verdict = judge_dispute(case.record, UnavailableLlm())

        # Assert
        assert verdict.llm_status == "unavailable"
        assert verdict.verdict == case.expected_verdict
        assert verdict.rubric_verdict == case.expected_verdict
        assert [reason.code for reason in verdict.reasons] == failed_codes(case.record)


class TestModelAndScope:
    def test_model_name_comes_from_the_llm_response(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        llm = ScriptedLlm(text=reply_json("valid"), model="scripted-model-7")

        # Act
        verdict = judge_dispute(record, llm)

        # Assert
        assert verdict.model == "scripted-model-7"
        assert verdict.llm_status == "used"

    def test_record_is_unchanged_by_judging(self) -> None:
        # Arrange
        record = CASES[NEEDS_REVIEW_CASE].record
        before = copy.deepcopy(record)
        fake = FakeJudgeLlm(reply=reply_json("needs_review", "odd"))

        # Act
        judge_dispute(record, fake)

        # Assert
        assert record == before

    def test_dispute_id_ignores_another_uuid_mentioned_by_the_llm(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        other = "00000000-0000-4000-8000-0000000009ff"
        fake = FakeJudgeLlm(reply=reply_json("needs_review", f"this looks like dispute {other}"))

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "used"
        assert str(verdict.dispute_id) == "00000000-0000-4000-8000-000000000101"
        assert verdict.dispute_id == record.dispute_id

    def test_reply_that_tries_to_set_dispute_id_is_rejected_and_id_stays(self) -> None:
        # Arrange
        record = CASES[VALID_CASE].record
        reply = json.dumps(
            {"verdict": "needs_review", "reasons": [], "dispute_id": "00000000-0000-4000-8000-0000000009ff"}
        )
        fake = FakeJudgeLlm(reply=reply)

        # Act
        verdict = judge_dispute(record, fake)

        # Assert
        assert verdict.llm_status == "rejected"
        assert verdict.dispute_id == record.dispute_id
