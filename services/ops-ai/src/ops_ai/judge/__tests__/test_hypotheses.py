"""Tests for ops_ai/judge/hypotheses.py, written from docs/specs/evaluation-judge.md (6.2 and 6.3)."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.judge.hypotheses import (
    PLAYBOOK,
    HypothesisRanking,
    RankedHypothesis,
    rank_hypotheses,
    ref_weight,
)
from ops_ai.judge.schemas import Hypothesis
from pydantic import ValidationError

JUDGE_DATA_DIR = Path(__file__).resolve().parents[4] / "eval" / "judge"

PLAYBOOK_CODES = [
    "issuer_outage_on_bin",
    "acquirer_outage",
    "elevated_failures",
    "merchant_integration_error",
    "insufficient_funds",
    "do_not_honor",
    "expired_card",
    "incorrect_cvc",
    "fraud_flagged",
    "card_testing",
    "cross_border_limit_for_kyc_tier",
]
INCIDENT_FIX_STEP = (
    "Apply the fix from incident inc_0001: Rerouted BIN 400000 to acquirer acq_b until the issuer recovered."
)


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


def rank_case(case_id: str) -> HypothesisRanking:
    case = CASES[case_id]
    return rank_hypotheses(case.hypotheses, case.evidence)


def by_id(ranking: HypothesisRanking) -> dict[str, RankedHypothesis]:
    return {item.hypothesis_id: item for item in ranking.ranked}


def hyp(hypothesis_id: str, cause_code: str, *refs: str) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis_id,
        cause_code=cause_code,
        statement=f"Statement for {hypothesis_id}.",
        evidence_refs=refs,
    )


def item(ref: str, summary: str = "summary") -> EvidenceItem:
    return EvidenceItem(ref=ref, summary=summary)


def hypothesis_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "hypothesis_id": "h_1",
        "cause_code": "acquirer_outage",
        "statement": "The acquirer is down.",
        "evidence_refs": ["aggregate:window"],
    }
    data.update(overrides)
    return data


class TestRefWeight:
    @pytest.mark.parametrize(
        ("ref", "weight"),
        [
            ("aggregate:bin:400000", 3.0),
            ("aggregate:bin:", 3.0),
            ("aggregate:window", 2.0),
            ("incident:inc_0001", 2.0),
            ("aggregate:baseline", 1.0),
            ("payment:00000000-0000-4000-8000-000000000401", 0.5),
            ("episode:00000000-0000-4000-8000-000000000401", 1.0),
            ("aggregate:decline_code:issuer_unavailable", 1.0),
            ("aggregate:bin", 1.0),
            ("INCIDENT:inc_0001", 1.0),
            ("ticket:t_1", 1.0),
            ("", 1.0),
        ],
    )
    def test_weight_is_chosen_by_prefix(self, ref: str, weight: float) -> None:
        # Arrange
        expected = weight

        # Act
        actual = ref_weight(ref)

        # Assert
        assert actual == expected


class TestPlaybook:
    def test_playbook_has_exactly_the_spec_cause_codes(self) -> None:
        # Arrange
        expected = set(PLAYBOOK_CODES)

        # Act
        codes = set(PLAYBOOK)

        # Assert
        assert codes == expected

    @pytest.mark.parametrize("code", PLAYBOOK_CODES)
    def test_each_entry_has_at_least_two_non_empty_steps(self, code: str) -> None:
        # Arrange
        steps = PLAYBOOK[code]

        # Act
        blank = [step for step in steps if step.strip() == ""]

        # Assert
        assert len(steps) >= 2
        assert blank == []


class TestSq1BinOutage:
    def test_order_is_bin_then_acquirer_then_integration_then_fabricated(self) -> None:
        # Arrange / Act
        ranking = rank_case("sq1_bin_outage")

        # Assert
        assert [r.hypothesis_id for r in ranking.ranked] == ["h_bin", "h_acq", "h_int", "h_fab"]

    def test_scores_and_ranks(self) -> None:
        # Arrange / Act
        ranking = rank_case("sq1_bin_outage")

        # Assert
        assert [r.support_score for r in ranking.ranked] == [7.0, 2.0, 0.0, -2.0]
        assert [r.rank for r in ranking.ranked] == [1, 2, 3, 4]

    def test_supported_flags(self) -> None:
        # Arrange / Act
        ranking = rank_case("sq1_bin_outage")

        # Assert
        assert [r.supported for r in ranking.ranked] == [True, True, False, False]

    def test_fabricated_incident_ref_is_flagged_and_not_valid(self) -> None:
        # Arrange / Act
        fab = by_id(rank_case("sq1_bin_outage"))["h_fab"]

        # Assert
        assert fab.fabricated_refs == ("incident:inc_4242",)
        assert fab.valid_refs == ()

    def test_valid_refs_keep_citation_order(self) -> None:
        # Arrange / Act
        ranked = by_id(rank_case("sq1_bin_outage"))

        # Assert
        assert ranked["h_bin"].valid_refs == ("aggregate:bin:400000", "aggregate:window", "incident:inc_0001")
        assert ranked["h_bin"].fabricated_refs == ()
        assert ranked["h_acq"].valid_refs == ("aggregate:window",)
        assert ranked["h_int"].valid_refs == ()
        assert ranked["h_int"].fabricated_refs == ()

    def test_bin_resolution_steps_are_playbook_plus_incident_fix(self) -> None:
        # Arrange / Act
        bin_hypothesis = by_id(rank_case("sq1_bin_outage"))["h_bin"]

        # Assert
        assert bin_hypothesis.resolution_steps == (*PLAYBOOK["issuer_outage_on_bin"], INCIDENT_FIX_STEP)

    def test_hypotheses_without_a_valid_incident_get_only_the_playbook(self) -> None:
        # Arrange / Act
        ranked = by_id(rank_case("sq1_bin_outage"))

        # Assert
        assert ranked["h_acq"].resolution_steps == PLAYBOOK["acquirer_outage"]
        assert ranked["h_int"].resolution_steps == PLAYBOOK["merchant_integration_error"]
        assert ranked["h_fab"].resolution_steps == PLAYBOOK["acquirer_outage"]

    def test_statements_and_cause_codes_are_passed_through(self) -> None:
        # Arrange
        case = CASES["sq1_bin_outage"]

        # Act
        ranked = by_id(rank_hypotheses(case.hypotheses, case.evidence))

        # Assert
        for hypothesis in case.hypotheses:
            assert ranked[hypothesis.hypothesis_id].statement == hypothesis.statement
            assert ranked[hypothesis.hypothesis_id].cause_code == hypothesis.cause_code


class TestSq3CardTesting:
    def test_card_testing_scores_one_point_five_and_leads(self) -> None:
        # Arrange / Act
        ranking = rank_case("sq3_card_testing")

        # Assert
        assert [r.hypothesis_id for r in ranking.ranked] == ["h_ct", "h_funds"]
        assert [r.support_score for r in ranking.ranked] == [1.5, 0.0]
        assert [r.supported for r in ranking.ranked] == [True, False]
        assert ranking.ranked[0].resolution_steps == PLAYBOOK["card_testing"]


class TestScoring:
    def test_duplicate_valid_refs_are_counted_once(self) -> None:
        # Arrange
        hypotheses = [hyp("h_dup", "acquirer_outage", "aggregate:window", "aggregate:window")]
        evidence = [item("aggregate:window")]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.support_score == 2.0
        assert ranked.valid_refs == ("aggregate:window",)

    def test_duplicate_fabricated_refs_are_counted_once(self) -> None:
        # Arrange
        hypotheses = [hyp("h_dup", "acquirer_outage", "incident:nope", "incident:nope")]

        # Act
        ranked = rank_hypotheses(hypotheses, []).ranked[0]

        # Assert
        assert ranked.support_score == -2.0
        assert ranked.fabricated_refs == ("incident:nope",)

    def test_one_fabricated_ref_costs_two_points_and_voids_support(self) -> None:
        # Arrange
        hypotheses = [hyp("h_mix", "acquirer_outage", "payment:p1", "incident:nope")]
        evidence = [item("payment:p1")]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.support_score == -1.5
        assert ranked.valid_refs == ("payment:p1",)
        assert ranked.fabricated_refs == ("incident:nope",)
        assert ranked.supported is False

    def test_valid_refs_follow_citation_order_not_evidence_order(self) -> None:
        # Arrange
        hypotheses = [hyp("h_1", "acquirer_outage", "incident:inc_b", "aggregate:window", "payment:p1")]
        evidence = [item("payment:p1"), item("aggregate:window"), item("incident:inc_b")]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.valid_refs == ("incident:inc_b", "aggregate:window", "payment:p1")
        assert ranked.support_score == 4.5

    def test_score_difference_beats_input_order(self) -> None:
        # Arrange
        hypotheses = [
            hyp("h_low", "elevated_failures", "payment:p1"),
            hyp("h_high", "acquirer_outage", "aggregate:window"),
        ]
        evidence = [item("payment:p1"), item("aggregate:window")]

        # Act
        ranking = rank_hypotheses(hypotheses, evidence)

        # Assert
        assert [r.hypothesis_id for r in ranking.ranked] == ["h_high", "h_low"]
        assert [r.rank for r in ranking.ranked] == [1, 2]

    def test_ties_keep_input_order_for_three_hypotheses(self) -> None:
        # Arrange
        hypotheses = [
            hyp("h_c", "elevated_failures", "aggregate:window"),
            hyp("h_a", "acquirer_outage", "incident:i1"),
            hyp("h_b", "do_not_honor", "aggregate:window"),
        ]
        evidence = [item("aggregate:window"), item("incident:i1")]

        # Act
        ranking = rank_hypotheses(hypotheses, evidence)

        # Assert
        assert [r.hypothesis_id for r in ranking.ranked] == ["h_c", "h_a", "h_b"]
        assert [r.support_score for r in ranking.ranked] == [2.0, 2.0, 2.0]


class TestResolutionSteps:
    def test_unknown_cause_code_asks_for_more_evidence(self) -> None:
        # Arrange
        hypotheses = [hyp("h_x", "mystery_cause")]

        # Act
        ranked = rank_hypotheses(hypotheses, []).ranked[0]

        # Assert
        assert ranked.resolution_steps == (
            "Collect more evidence before acting: no playbook entry for mystery_cause.",
        )

    def test_incident_without_fix_text_adds_no_step(self) -> None:
        # Arrange
        hypotheses = [hyp("h_1", "acquirer_outage", "incident:inc_7")]
        evidence = [item("incident:inc_7", "Acquirer degraded for an hour.")]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.resolution_steps == PLAYBOOK["acquirer_outage"]

    def test_fixes_from_several_incidents_follow_citation_order(self) -> None:
        # Arrange
        hypotheses = [hyp("h_1", "acquirer_outage", "incident:inc_b", "incident:inc_a")]
        evidence = [
            item("incident:inc_a", "Outage A. Fix: Restart A."),
            item("incident:inc_b", "Outage B. Fix: Restart B."),
        ]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.resolution_steps == (
            *PLAYBOOK["acquirer_outage"],
            "Apply the fix from incident inc_b: Restart B.",
            "Apply the fix from incident inc_a: Restart A.",
        )

    def test_fix_text_in_a_non_incident_summary_is_ignored(self) -> None:
        # Arrange
        hypotheses = [hyp("h_1", "acquirer_outage", "aggregate:window")]
        evidence = [item("aggregate:window", "36 of 240 failed. Fix: Do something else.")]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.resolution_steps == PLAYBOOK["acquirer_outage"]

    def test_fix_from_an_uncited_incident_is_ignored(self) -> None:
        # Arrange
        hypotheses = [hyp("h_1", "acquirer_outage", "aggregate:window")]
        evidence = [item("aggregate:window"), item("incident:inc_9", "Outage. Fix: Restart.")]

        # Act
        ranked = rank_hypotheses(hypotheses, evidence).ranked[0]

        # Assert
        assert ranked.resolution_steps == PLAYBOOK["acquirer_outage"]


class TestRankingContract:
    def test_duplicate_hypothesis_id_raises_value_error(self) -> None:
        # Arrange
        hypotheses = [hyp("h_same", "acquirer_outage"), hyp("h_same", "elevated_failures")]

        # Act / Assert
        with pytest.raises(ValueError):
            rank_hypotheses(hypotheses, [])

    def test_empty_input_gives_empty_ranking_without_llm(self) -> None:
        # Arrange
        evidence = [item("aggregate:window")]

        # Act
        ranking = rank_hypotheses([], evidence)

        # Assert
        assert ranking.ranked == ()
        assert ranking.llm_status == "skipped"
        assert ranking.model is None

    def test_without_llm_status_is_skipped_and_no_rationales(self) -> None:
        # Arrange / Act
        ranking = rank_case("sq1_bin_outage")

        # Assert
        assert ranking.llm_status == "skipped"
        assert ranking.model is None
        assert [r.llm_rationale for r in ranking.ranked] == [None, None, None, None]

    def test_ranked_is_a_tuple(self) -> None:
        # Arrange / Act
        ranking = rank_case("sq1_bin_outage")

        # Assert
        assert type(ranking.ranked) is tuple

    @pytest.mark.parametrize("case", [pytest.param(c, id=c.case_id) for c in CASES.values()])
    def test_golden_case_matches_expected_order(self, case: HypothesisCase) -> None:
        # Arrange
        hypotheses = case.hypotheses
        evidence = case.evidence

        # Act
        ranking = rank_hypotheses(hypotheses, evidence)

        # Assert
        assert [r.hypothesis_id for r in ranking.ranked] == case.expected_order
        assert [r.rank for r in ranking.ranked] == list(range(1, len(case.expected_order) + 1))

    def test_tie_case_keeps_input_order(self) -> None:
        # Arrange / Act
        ranking = rank_case("tie_keeps_input_order")

        # Assert
        assert [r.hypothesis_id for r in ranking.ranked] == ["h_first", "h_second"]
        assert [r.support_score for r in ranking.ranked] == [2.0, 2.0]


class TestHypothesisSchema:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"hypothesis_id": "a-b_C9"},
            {"hypothesis_id": "a" * 64},
            {"statement": "s" * 500},
            {"cause_code": "c" * 64},
            {"evidence_refs": [f"payment:{i}" for i in range(20)]},
            {"evidence_refs": []},
        ],
    )
    def test_boundary_values_are_accepted(self, overrides: dict[str, object]) -> None:
        # Arrange
        data = hypothesis_data(**overrides)

        # Act
        hypothesis = Hypothesis.model_validate(data)

        # Assert
        for key, value in overrides.items():
            expected = tuple(value) if isinstance(value, list) else value
            assert getattr(hypothesis, key) == expected

    @pytest.mark.parametrize(
        "overrides",
        [
            {"hypothesis_id": "bad id"},
            {"hypothesis_id": ""},
            {"hypothesis_id": "a" * 65},
            {"hypothesis_id": "h/1"},
            {"statement": "s" * 501},
            {"cause_code": "c" * 65},
            {"evidence_refs": [f"payment:{i}" for i in range(21)]},
            {"extra_field": "nope"},
        ],
    )
    def test_out_of_range_values_are_rejected(self, overrides: dict[str, object]) -> None:
        # Arrange
        data = hypothesis_data(**overrides)

        # Act / Assert
        with pytest.raises(ValidationError):
            Hypothesis.model_validate(data)

    @pytest.mark.parametrize("missing", ["hypothesis_id", "cause_code", "statement"])
    def test_missing_required_field_is_rejected(self, missing: str) -> None:
        # Arrange
        data = hypothesis_data()
        del data[missing]

        # Act / Assert
        with pytest.raises(ValidationError):
            Hypothesis.model_validate(data)
