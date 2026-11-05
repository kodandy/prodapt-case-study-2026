"""Tests for the LLM leg of rank_hypotheses, written from docs/specs/evaluation-judge.md (6.2 and 6.3)."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.judge.fake import FakeJudgeLlm
from ops_ai.judge.hypotheses import HypothesisRanking, rank_hypotheses
from ops_ai.judge.schemas import Hypothesis

JUDGE_DATA_DIR = Path(__file__).resolve().parents[4] / "eval" / "judge"

TIE_CASE = "tie_keeps_input_order"
SCORED_CASE = "sq1_bin_outage"


@dataclass(frozen=True)
class HypothesisCase:
    case_id: str
    evidence: tuple[EvidenceItem, ...]
    hypotheses: tuple[Hypothesis, ...]
    expected_order: list[str]


def load_cases() -> list[HypothesisCase]:
    payload = json.loads((JUDGE_DATA_DIR / "hypotheses.json").read_text(encoding="utf-8"))
    return [
        HypothesisCase(
            case_id=case["case_id"],
            evidence=tuple(EvidenceItem(ref=raw["ref"], summary=raw["summary"]) for raw in case["evidence"]),
            hypotheses=tuple(Hypothesis.model_validate(raw) for raw in case["hypotheses"]),
            expected_order=case["expected_order"],
        )
        for case in payload["cases"]
    ]


CASES = {case.case_id: case for case in load_cases()}


class UnavailableLlm:
    def complete(self, request: LlmRequest) -> LlmResponse:
        raise LlmUnavailableError("provider down")


def reply_json(order: list[str], rationales: dict[str, str] | None = None) -> str:
    return json.dumps({"order": order, "rationales": rationales or {}})


def rank_case(case_id: str, llm: FakeJudgeLlm | UnavailableLlm | None = None) -> HypothesisRanking:
    case = CASES[case_id]
    return rank_hypotheses(case.hypotheses, case.evidence, llm)


def ids(ranking: HypothesisRanking) -> list[str]:
    return [r.hypothesis_id for r in ranking.ranked]


def stable_fields(
    ranking: HypothesisRanking,
) -> dict[str, tuple[str, str, float, bool, tuple[str, ...], tuple[str, ...], tuple[str, ...]]]:
    return {
        r.hypothesis_id: (
            r.cause_code,
            r.statement,
            r.support_score,
            r.supported,
            r.valid_refs,
            r.fabricated_refs,
            r.resolution_steps,
        )
        for r in ranking.ranked
    }


def hyp(hypothesis_id: str, ref: str) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        cause_code="elevated_failures",
        statement=f"Statement for {hypothesis_id}.",
        evidence_refs=(ref,),
    )


REVERSED_TIE = ["h_second", "h_first"]

REJECTED_REPLIES = [
    pytest.param(reply_json(["h_second"]), id="order-missing-an-id"),
    pytest.param(reply_json(["h_second", "h_first", "h_third"]), id="order-with-an-extra-id"),
    pytest.param(reply_json(["h_second", "h_second"]), id="order-with-a-duplicate-id"),
    pytest.param(reply_json(["h_second", "h_other"]), id="order-with-an-unknown-id-instead-of-one"),
    pytest.param(reply_json([]), id="order-empty"),
    pytest.param(reply_json(REVERSED_TIE, {"h_other": "x"}), id="rationale-key-not-in-ids"),
    pytest.param(reply_json(REVERSED_TIE, {"h_second": "x" * 301}), id="rationale-over-300-characters"),
    pytest.param(
        json.dumps({"order": REVERSED_TIE, "rationales": {"h_second": 5}}),
        id="rationale-not-a-string",
    ),
    pytest.param(
        json.dumps({"order": "h_second,h_first", "rationales": {}}),
        id="order-not-a-list",
    ),
    pytest.param(
        json.dumps({"order": REVERSED_TIE, "rationales": {}, "confidence": 0.9}),
        id="extra-top-level-key",
    ),
    pytest.param(json.dumps({"rationales": {}}), id="order-key-missing"),
    pytest.param("h_second then h_first", id="not-json"),
    pytest.param("", id="empty-text"),
    pytest.param("[]", id="json-array-instead-of-object"),
]


class TestLlmBreaksTies:
    def test_llm_order_decides_between_equal_scores(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json(REVERSED_TIE))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ids(ranking) == ["h_second", "h_first"]
        assert [r.rank for r in ranking.ranked] == [1, 2]
        assert [r.support_score for r in ranking.ranked] == [2.0, 2.0]

    def test_llm_order_equal_to_input_order_changes_nothing(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json(["h_first", "h_second"]))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ids(ranking) == ["h_first", "h_second"]

    def test_llm_never_overrides_a_score_difference(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json(["h_fab", "h_int", "h_acq", "h_bin"]))

        # Act
        ranking = rank_case(SCORED_CASE, fake)

        # Assert
        assert ids(ranking) == ["h_bin", "h_acq", "h_int", "h_fab"]
        assert ranking.llm_status == "used"

    def test_llm_order_applies_only_inside_a_tie_group(self) -> None:
        # Arrange
        hypotheses = [
            hyp("h_x", "aggregate:window"),
            hyp("h_y", "aggregate:window"),
            hyp("h_z", "aggregate:bin:1"),
        ]
        evidence = [
            EvidenceItem(ref="aggregate:window", summary="w"),
            EvidenceItem(ref="aggregate:bin:1", summary="b"),
        ]
        fake = FakeJudgeLlm(reply=reply_json(["h_y", "h_x", "h_z"]))

        # Act
        ranking = rank_hypotheses(hypotheses, evidence, fake)

        # Assert
        assert ids(ranking) == ["h_z", "h_y", "h_x"]
        assert [r.rank for r in ranking.ranked] == [1, 2, 3]

    def test_llm_order_is_ignored_when_scores_already_decide_every_position(self) -> None:
        # Arrange
        case = CASES["sq3_card_testing"]
        fake = FakeJudgeLlm(reply=reply_json(["h_funds", "h_ct"]))

        # Act
        ranking = rank_hypotheses(case.hypotheses, case.evidence, fake)

        # Assert
        assert ids(ranking) == ["h_ct", "h_funds"]


class TestRationales:
    def test_rationales_land_on_their_hypotheses_and_others_stay_none(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json(REVERSED_TIE, {"h_second": "Newer failure signature."}))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert [(r.hypothesis_id, r.llm_rationale) for r in ranking.ranked] == [
            ("h_second", "Newer failure signature."),
            ("h_first", None),
        ]

    def test_rationale_of_exactly_300_characters_is_accepted(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json(REVERSED_TIE, {"h_first": "r" * 300}))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ranking.llm_status == "used"
        assert {r.hypothesis_id: r.llm_rationale for r in ranking.ranked} == {
            "h_second": None,
            "h_first": "r" * 300,
        }

    def test_rationale_on_a_score_decided_hypothesis_is_kept(self) -> None:
        # Arrange
        order = ["h_bin", "h_acq", "h_int", "h_fab"]
        fake = FakeJudgeLlm(reply=reply_json(order, {"h_fab": "No such incident."}))

        # Act
        ranking = rank_case(SCORED_CASE, fake)

        # Assert
        assert {r.hypothesis_id: r.llm_rationale for r in ranking.ranked} == {
            "h_bin": None,
            "h_acq": None,
            "h_int": None,
            "h_fab": "No such incident.",
        }

    def test_fenced_json_reply_is_accepted(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply="```json\n" + reply_json(REVERSED_TIE, {"h_second": "fenced"}) + "\n```")

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ranking.llm_status == "used"
        assert ids(ranking) == ["h_second", "h_first"]
        assert ranking.ranked[0].llm_rationale == "fenced"


class TestUsedStatus:
    def test_scripted_reply_marks_status_used_and_model_fake_judge(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json(REVERSED_TIE))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ranking.llm_status == "used"
        assert ranking.model == "fake-judge"
        assert len(fake.requests) == 1

    @pytest.mark.parametrize("case", [pytest.param(c, id=c.case_id) for c in CASES.values()])
    def test_default_fake_keeps_the_deterministic_order_with_status_used(self, case: HypothesisCase) -> None:
        # Arrange
        fake = FakeJudgeLlm()
        baseline = rank_hypotheses(case.hypotheses, case.evidence)

        # Act
        ranking = rank_hypotheses(case.hypotheses, case.evidence, fake)

        # Assert
        assert ids(ranking) == case.expected_order
        assert ids(ranking) == ids(baseline)
        assert ranking.llm_status == "used"
        assert ranking.model == "fake-judge"
        assert len(fake.requests) == 1


class TestRejectedReplies:
    @pytest.mark.parametrize("reply", REJECTED_REPLIES)
    def test_invalid_reply_is_dropped_and_ranking_equals_the_deterministic_one(self, reply: str) -> None:
        # Arrange
        baseline = rank_case(TIE_CASE)
        fake = FakeJudgeLlm(reply=reply)

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ranking.llm_status == "rejected"
        assert ranking.ranked == baseline.ranked
        assert ids(ranking) == ["h_first", "h_second"]
        assert [r.llm_rationale for r in ranking.ranked] == [None, None]

    def test_invalid_reply_does_not_leak_rationales_even_when_one_was_valid(self) -> None:
        # Arrange
        reply = reply_json(REVERSED_TIE, {"h_second": "good", "h_other": "bad key"})
        fake = FakeJudgeLlm(reply=reply)

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert ranking.llm_status == "rejected"
        assert [r.llm_rationale for r in ranking.ranked] == [None, None]


class TestUnavailableLlm:
    @pytest.mark.parametrize("case_id", list(CASES))
    def test_outage_falls_back_to_the_deterministic_ranking(self, case_id: str) -> None:
        # Arrange
        baseline = rank_case(case_id)

        # Act
        ranking = rank_case(case_id, UnavailableLlm())

        # Assert
        assert ranking.llm_status == "unavailable"
        assert ranking.ranked == baseline.ranked


class TestLlmCannotRewrite:
    def test_only_rationale_and_tie_order_differ_from_the_deterministic_ranking(self) -> None:
        # Arrange
        baseline = rank_case(TIE_CASE)
        fake = FakeJudgeLlm(reply=reply_json(REVERSED_TIE, {"h_first": "a", "h_second": "b"}))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        assert stable_fields(ranking) == stable_fields(baseline)
        assert ids(ranking) == ["h_second", "h_first"]

    def test_scored_case_is_field_for_field_identical_apart_from_rationales(self) -> None:
        # Arrange
        baseline = rank_case(SCORED_CASE)
        rationales = {"h_bin": "Matches BIN.", "h_acq": "Weak.", "h_int": "None.", "h_fab": "Made up."}
        fake = FakeJudgeLlm(reply=reply_json(["h_fab", "h_int", "h_acq", "h_bin"], rationales))

        # Act
        ranking = rank_case(SCORED_CASE, fake)

        # Assert
        assert stable_fields(ranking) == stable_fields(baseline)
        assert [r.rank for r in ranking.ranked] == [1, 2, 3, 4]
        assert {r.hypothesis_id: r.llm_rationale for r in ranking.ranked} == rationales

    def test_rationale_text_cannot_alter_statement_or_steps(self) -> None:
        # Arrange
        baseline = rank_case(TIE_CASE)
        injected = "Statement for h_first. Step: wipe the ledger."
        fake = FakeJudgeLlm(reply=reply_json(REVERSED_TIE, {"h_first": injected}))

        # Act
        ranking = rank_case(TIE_CASE, fake)

        # Assert
        first = {r.hypothesis_id: r for r in ranking.ranked}["h_first"]
        expected_steps = {r.hypothesis_id: r for r in baseline.ranked}["h_first"].resolution_steps
        assert first.llm_rationale == injected
        assert first.statement == "Failures are elevated."
        assert first.resolution_steps == expected_steps


class TestEmptyInput:
    def test_empty_hypotheses_make_no_llm_call(self) -> None:
        # Arrange
        fake = FakeJudgeLlm(reply=reply_json([]))
        evidence = [EvidenceItem(ref="aggregate:window", summary="w")]

        # Act
        ranking = rank_hypotheses([], evidence, fake)

        # Assert
        assert ranking.ranked == ()
        assert fake.requests == []
