"""Golden cases: loading and scoring one response (docs/specs/evaluation-judge.md section 4)."""

import copy
import json
from pathlib import Path

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.evaluation.golden import CaseResult, GoldenCase, load_golden_cases, score_case
from ops_ai.evaluation.metrics import MetricResult, gate, not_applicable, skipped

GOLDEN_DIR = Path(__file__).resolve().parents[4] / "eval" / "golden"
FOREIGN = frozenset({"mch_foreign"})
ORDER_WITHOUT_HYPOTHESES = [
    "status",
    "faithfulness",
    "answer_relevancy",
    "evidence_ref_coverage",
    "cross_tenant_leakage",
    "llm_faithfulness",
    "llm_answer_relevancy",
]
ORDER_WITH_HYPOTHESES = [
    "status",
    "faithfulness",
    "answer_relevancy",
    "evidence_ref_coverage",
    "cross_tenant_leakage",
    "hypothesis_ranking",
    "llm_faithfulness",
    "llm_answer_relevancy",
]
INCIDENT_SUMMARY = (
    "Issuer outage on BIN 400000 during the afternoon. "
    "Fix: Rerouted BIN 400000 to acquirer acq_b until the issuer recovered."
)


class _StubJudge:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.requests: list[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(text=self.reply, model="stub-judge")


def _case_dict() -> dict[str, object]:
    return {
        "case_id": "c1",
        "sample_query": "SQ1",
        "route": "/v1/assistant/investigate",
        "principal": "sq1_merchant",
        "body": {"question": "why are payments failing?"},
        "expect": {
            "status": 200,
            "fields": {"intent": "failure_spike"},
            "evidence_ref_prefixes": ["aggregate:window", "incident:"],
            "required_terms": ["400000"],
            "tool_calls": ["aggregate_failures", "compare_baseline"],
        },
        "thresholds": {"faithfulness": 0.9, "answer_relevancy": 1.0, "evidence_ref_coverage": 1.0},
    }


def _case(**expect_overrides: object) -> GoldenCase:
    data = _case_dict()
    expect = copy.deepcopy(data["expect"])
    assert isinstance(expect, dict)
    expect.update(expect_overrides)
    data["expect"] = expect
    return GoldenCase.model_validate(data)


def _body() -> dict[str, object]:
    return {
        "intent": "failure_spike",
        "text": "BIN 400000 failed 32 of 40 payments [aggregate:window]. Past outage [incident:inc_0001].",
        "suggested_fixes": [],
        "tool_calls": [
            {"name": "aggregate_failures", "args": {}},
            {"name": "compare_baseline", "args": {}},
        ],
        "evidence": [
            {"ref": "aggregate:window", "summary": "Window: 40 card payments, 32 failed on BIN 400000"},
            {"ref": "incident:inc_0001", "summary": "Issuer outage on BIN 400000"},
        ],
    }


def _by_name(result: CaseResult) -> dict[str, MetricResult]:
    return {metric.name: metric for metric in result.metrics}


def _loaded(case_id: str) -> GoldenCase:
    return next(case for case in load_golden_cases(GOLDEN_DIR) if case.case_id == case_id)


def _write_case(directory: Path, filename: str, data: dict[str, object]) -> None:
    (directory / filename).write_text(json.dumps(data))


def test_load_golden_cases_returns_the_seven_shipped_cases_sorted_by_case_id() -> None:
    # Arrange / Act
    cases = load_golden_cases(GOLDEN_DIR)

    # Assert
    assert [case.case_id for case in cases] == [
        "isolation_quiet_merchant",
        "isolation_user_on_merchant_route",
        "sq1_bin_outage",
        "sq1_failure_route",
        "sq2_cross_border_limit",
        "sq3_card_testing",
        "sq4_settlement_gap",
    ]


def test_load_golden_cases_covers_the_four_sample_queries() -> None:
    # Arrange / Act
    cases = load_golden_cases(GOLDEN_DIR)

    # Assert
    assert {case.sample_query for case in cases} == {"SQ1", "SQ2", "SQ3", "SQ4"}


def test_load_golden_cases_reads_route_and_principal_of_a_case() -> None:
    # Arrange / Act
    case = _loaded("sq2_cross_border_limit")

    # Assert
    assert (case.route, case.principal) == ("/v1/assistant/investigate-transfer", "sq2_user")


def test_load_golden_cases_sorts_by_case_id_not_by_file_name(tmp_path: Path) -> None:
    # Arrange
    first = _case_dict() | {"case_id": "a_case"}
    second = _case_dict() | {"case_id": "z_case"}
    _write_case(tmp_path, "a.json", second)
    _write_case(tmp_path, "z.json", first)

    # Act
    cases = load_golden_cases(tmp_path)

    # Assert
    assert [case.case_id for case in cases] == ["a_case", "z_case"]


def test_load_golden_cases_ignores_files_that_are_not_json(tmp_path: Path) -> None:
    # Arrange
    _write_case(tmp_path, "c1.json", _case_dict())
    (tmp_path / "notes.txt").write_text("not a case")

    # Act
    cases = load_golden_cases(tmp_path)

    # Assert
    assert [case.case_id for case in cases] == ["c1"]


def test_load_golden_cases_rejects_a_duplicate_case_id_and_names_a_file(tmp_path: Path) -> None:
    # Arrange
    _write_case(tmp_path, "first.json", _case_dict())
    _write_case(tmp_path, "second.json", _case_dict())

    # Act
    with pytest.raises(ValueError) as excinfo:
        load_golden_cases(tmp_path)

    # Assert
    message = str(excinfo.value)
    assert "first.json" in message or "second.json" in message


def test_load_golden_cases_rejects_an_invalid_file_and_names_it(tmp_path: Path) -> None:
    # Arrange
    _write_case(tmp_path, "good_case.json", _case_dict())
    _write_case(tmp_path, "bad_case.json", _case_dict() | {"case_id": "c2", "unexpected": 1})

    # Act / Assert
    with pytest.raises(ValueError, match=r"bad_case\.json"):
        load_golden_cases(tmp_path)


def test_load_golden_cases_rejects_a_file_that_is_not_json_and_names_it(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / "broken_case.json").write_text("{not json")

    # Act / Assert
    with pytest.raises(ValueError, match=r"broken_case\.json"):
        load_golden_cases(tmp_path)


def test_case_result_passes_when_no_metric_failed() -> None:
    # Arrange
    metrics = (
        gate("status", 1.0, 1.0),
        skipped("llm_faithfulness", 0.7, "no key"),
        not_applicable("faithfulness", 0.9, "no narration"),
    )

    # Act
    result = CaseResult(case_id="c1", sample_query="SQ1", metrics=metrics)

    # Assert
    assert result.passed is True


def test_case_result_fails_when_any_metric_failed() -> None:
    # Arrange
    metrics = (gate("status", 1.0, 1.0), gate("answer_relevancy", 0.5, 1.0))

    # Act
    result = CaseResult(case_id="c1", sample_query="SQ1", metrics=metrics)

    # Assert
    assert result.passed is False


def test_score_case_returns_the_metrics_in_the_specified_order() -> None:
    # Arrange
    case = _case()

    # Act
    result = score_case(case, 200, _body(), FOREIGN)

    # Assert
    assert [metric.name for metric in result.metrics] == ORDER_WITHOUT_HYPOTHESES
    assert (result.case_id, result.sample_query) == ("c1", "SQ1")


def test_score_case_passes_every_deterministic_metric_for_a_faithful_relevant_response() -> None:
    # Arrange
    case = _case()

    # Act
    result = score_case(case, 200, _body(), FOREIGN)

    # Assert
    statuses = {metric.name: metric.status for metric in result.metrics}
    assert statuses == {
        "status": "passed",
        "faithfulness": "passed",
        "answer_relevancy": "passed",
        "evidence_ref_coverage": "passed",
        "cross_tenant_leakage": "passed",
        "llm_faithfulness": "skipped",
        "llm_answer_relevancy": "skipped",
    }
    assert result.passed is True


def test_score_case_scores_one_for_every_passed_deterministic_metric() -> None:
    # Arrange
    case = _case()

    # Act
    metrics = _by_name(score_case(case, 200, _body(), FOREIGN))

    # Assert
    scores = {name: metrics[name].score for name in ORDER_WITHOUT_HYPOTHESES[:5]}
    assert scores == {
        "status": 1.0,
        "faithfulness": 1.0,
        "answer_relevancy": 1.0,
        "evidence_ref_coverage": 1.0,
        "cross_tenant_leakage": 1.0,
    }


def test_score_case_fails_the_status_metric_on_a_different_status_code() -> None:
    # Arrange
    case = _case()

    # Act
    result = score_case(case, 500, _body(), FOREIGN)

    # Assert
    status = _by_name(result)["status"]
    assert (status.score, status.threshold, status.status) == (0.0, 1.0, "failed")
    assert result.passed is False


def test_score_case_passes_the_status_metric_for_an_expected_error_status() -> None:
    # Arrange
    case = _loaded("isolation_user_on_merchant_route")

    # Act
    result = score_case(case, 403, {"detail": "Forbidden"}, FOREIGN)

    # Assert
    status = _by_name(result)["status"]
    assert (status.score, status.threshold, status.status) == (1.0, 1.0, "passed")
    assert result.passed is True


def test_score_case_marks_faithfulness_not_applicable_when_the_text_is_null() -> None:
    # Arrange
    case = _case()
    body = _body() | {"text": None}

    # Act
    result = score_case(case, 200, body, FOREIGN)

    # Assert
    faithfulness = _by_name(result)["faithfulness"]
    assert (faithfulness.score, faithfulness.threshold, faithfulness.status) == (None, 0.9, "not_applicable")


def test_score_case_fails_faithfulness_when_the_text_cites_a_ref_missing_from_the_evidence() -> None:
    # Arrange: 5 of 6 claims supported, below the 0.9 threshold
    case = _case()
    body = _body()
    body["text"] = f"{body['text']} Also [payment:ghost]."

    # Act
    faithfulness = _by_name(score_case(case, 200, body, FOREIGN))["faithfulness"]

    # Assert
    assert faithfulness.status == "failed"
    assert faithfulness.score == pytest.approx(5 / 6)
    assert faithfulness.threshold == 0.9


def test_score_case_fails_answer_relevancy_when_the_tool_calls_are_in_a_different_order() -> None:
    # Arrange
    case = _case()
    body = _body()
    body["tool_calls"] = [
        {"name": "compare_baseline", "args": {}},
        {"name": "aggregate_failures", "args": {}},
    ]

    # Act
    relevancy = _by_name(score_case(case, 200, body, FOREIGN))["answer_relevancy"]

    # Assert
    assert relevancy.status == "failed"
    assert relevancy.score == pytest.approx(2 / 3)


def test_score_case_fails_answer_relevancy_when_a_field_differs() -> None:
    # Arrange
    case = _case()
    body = _body() | {"intent": "other"}

    # Act
    relevancy = _by_name(score_case(case, 200, body, FOREIGN))["answer_relevancy"]

    # Assert
    assert relevancy.status == "failed"
    assert relevancy.score == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("overrides", "expected_score"),
    [
        ({}, 1.0),
        ({"text": "no digits here", "summary": "BIN 400000 summary"}, 1.0),
        ({"text": "no digits here", "suggested_fixes": ["Reroute BIN 400000 to acq_b"]}, 1.0),
        ({"text": "no digits here"}, pytest.approx(2 / 3)),
    ],
    ids=["in-text", "in-summary", "in-suggested-fixes", "nowhere"],
)
def test_score_case_searches_the_required_terms_in_text_summary_and_suggested_fixes(
    overrides: dict[str, object], expected_score: float
) -> None:
    # Arrange
    case = _case()
    body = _body() | overrides

    # Act
    relevancy = _by_name(score_case(case, 200, body, FOREIGN))["answer_relevancy"]

    # Assert
    assert relevancy.score == expected_score


def test_score_case_resolves_dotted_field_paths_into_the_response() -> None:
    # Arrange
    case = _case(
        fields={"transfer.currency": "USD"},
        required_terms=[],
        tool_calls=["get_payment_status"],
        evidence_ref_prefixes=[],
    )
    body = {
        "transfer": {"currency": "USD", "amount_minor": 180000},
        "text": None,
        "tool_calls": [{"name": "get_payment_status", "args": {}}],
        "evidence": [],
    }

    # Act
    relevancy = _by_name(score_case(case, 200, body, FOREIGN))["answer_relevancy"]

    # Assert
    assert (relevancy.score, relevancy.status) == (1.0, "passed")


def test_score_case_fails_a_dotted_field_path_with_a_different_value() -> None:
    # Arrange
    case = _case(
        fields={"transfer.currency": "USD"},
        required_terms=[],
        tool_calls=["get_payment_status"],
        evidence_ref_prefixes=[],
    )
    body = {
        "transfer": {"currency": "EUR"},
        "text": None,
        "tool_calls": [{"name": "get_payment_status", "args": {}}],
        "evidence": [],
    }

    # Act
    relevancy = _by_name(score_case(case, 200, body, FOREIGN))["answer_relevancy"]

    # Assert
    assert (relevancy.score, relevancy.status) == (0.5, "failed")


def test_score_case_fails_a_dotted_field_path_whose_parent_is_missing_without_raising() -> None:
    # Arrange
    case = _case(
        fields={"transfer.currency": "USD"},
        required_terms=[],
        tool_calls=["get_payment_status"],
        evidence_ref_prefixes=[],
    )
    body = {"text": None, "tool_calls": [{"name": "get_payment_status", "args": {}}], "evidence": []}

    # Act
    relevancy = _by_name(score_case(case, 200, body, FOREIGN))["answer_relevancy"]

    # Assert
    assert (relevancy.score, relevancy.status) == (0.5, "failed")


def test_score_case_fails_evidence_ref_coverage_when_an_expected_prefix_is_not_covered() -> None:
    # Arrange
    case = _case()
    body = _body()
    body["evidence"] = [{"ref": "aggregate:window", "summary": "Window: 40 card payments, 32 failed"}]

    # Act
    coverage = _by_name(score_case(case, 200, body, FOREIGN))["evidence_ref_coverage"]

    # Assert
    assert (coverage.score, coverage.threshold, coverage.status) == (0.5, 1.0, "failed")


def test_score_case_applies_custom_thresholds_from_the_case() -> None:
    # Arrange
    data = _case_dict() | {
        "thresholds": {"faithfulness": 0.5, "answer_relevancy": 0.4, "evidence_ref_coverage": 0.3}
    }
    case = GoldenCase.model_validate(data)

    # Act
    metrics = _by_name(score_case(case, 200, _body(), FOREIGN))

    # Assert
    assert (
        metrics["faithfulness"].threshold,
        metrics["answer_relevancy"].threshold,
        metrics["evidence_ref_coverage"].threshold,
    ) == (0.5, 0.4, 0.3)


def test_score_case_defaults_missing_thresholds_to_one_and_faithfulness_to_point_nine() -> None:
    # Arrange
    case = GoldenCase.model_validate(_case_dict() | {"thresholds": {}})

    # Act
    metrics = _by_name(score_case(case, 200, _body(), FOREIGN))

    # Assert
    assert (
        metrics["status"].threshold,
        metrics["faithfulness"].threshold,
        metrics["answer_relevancy"].threshold,
        metrics["evidence_ref_coverage"].threshold,
        metrics["cross_tenant_leakage"].threshold,
    ) == (1.0, 0.9, 1.0, 1.0, 1.0)


def test_score_case_fails_the_case_when_a_foreign_token_is_in_the_body() -> None:
    # Arrange
    case = _case()
    body = _body()
    body["evidence"] = [
        {"ref": "aggregate:window", "summary": "Window: 40 payments, 32 failed on BIN 400000 mch_foreign"},
        {"ref": "incident:inc_0001", "summary": "Issuer outage on BIN 400000"},
    ]

    # Act
    result = score_case(case, 200, body, FOREIGN)

    # Assert
    leakage = _by_name(result)["cross_tenant_leakage"]
    assert (leakage.score, leakage.threshold, leakage.status) == (0.0, 1.0, "failed")
    assert "$.evidence[0].summary" in leakage.detail
    assert "mch_foreign" not in leakage.detail
    assert result.passed is False


def test_score_case_scores_an_error_body_without_text_or_evidence() -> None:
    # Arrange
    case = _loaded("isolation_user_on_merchant_route")

    # Act
    result = score_case(case, 403, {"detail": "Forbidden"}, FOREIGN)

    # Assert
    metrics = _by_name(result)
    assert metrics["faithfulness"].status == "not_applicable"
    assert metrics["evidence_ref_coverage"].status == "passed"
    assert metrics["cross_tenant_leakage"].status == "passed"
    assert result.passed is True


def test_score_case_adds_hypothesis_ranking_before_the_llm_metrics_when_the_case_has_hypotheses() -> None:
    # Arrange
    case = _loaded("sq1_bin_outage")

    # Act
    result = score_case(case, 200, _sq1_body(include_bin=True), FOREIGN)

    # Assert
    assert [metric.name for metric in result.metrics] == ORDER_WITH_HYPOTHESES


def _sq1_body(*, include_bin: bool) -> dict[str, object]:
    evidence: list[dict[str, str]] = [
        {"ref": "aggregate:window", "summary": "Window: 240 card payments, 36 failed"},
        {"ref": "aggregate:baseline", "summary": "Baseline: 420 card payments, 12 failed"},
    ]
    if include_bin:
        evidence.append({"ref": "aggregate:bin:400000", "summary": "BIN 400000: 32 of 40 failed"})
    evidence.append({"ref": "incident:inc_0001", "summary": INCIDENT_SUMMARY})
    return {
        "intent": "failure_spike",
        "cause_label": "issuer_outage_on_bin",
        "text": None,
        "suggested_fixes": [],
        "tool_calls": [],
        "evidence": evidence,
    }


def test_score_case_passes_hypothesis_ranking_when_the_ranked_ids_equal_the_expected_order() -> None:
    # Arrange: h_bin 3+2+2=7.0, h_acquirer 2.0, h_integration 1.0-2.0=-1.0
    case = _loaded("sq1_bin_outage")

    # Act
    ranking = _by_name(score_case(case, 200, _sq1_body(include_bin=True), FOREIGN))["hypothesis_ranking"]

    # Assert
    assert (ranking.score, ranking.threshold, ranking.status) == (1.0, 1.0, "passed")


def test_score_case_fails_hypothesis_ranking_when_the_bin_evidence_is_missing() -> None:
    # Arrange: h_bin 2+2-2=2.0 ties with h_acquirer 2.0, so the input order puts h_acquirer first
    case = _loaded("sq1_bin_outage")

    # Act
    ranking = _by_name(score_case(case, 200, _sq1_body(include_bin=False), FOREIGN))["hypothesis_ranking"]

    # Assert
    assert (ranking.score, ranking.threshold, ranking.status) == (0.0, 1.0, "failed")


def test_score_case_runs_the_llm_metrics_when_a_judge_is_given() -> None:
    # Arrange
    case = _case()
    judge = _StubJudge('{"score": 0.9, "reason": "grounded"}')

    # Act
    result = score_case(case, 200, _body(), FOREIGN, judge)

    # Assert
    metrics = _by_name(result)
    assert (metrics["llm_faithfulness"].status, metrics["llm_faithfulness"].score) == ("passed", 0.9)
    assert (metrics["llm_answer_relevancy"].status, metrics["llm_answer_relevancy"].score) == ("passed", 0.9)
    assert len(judge.requests) == 2


def test_score_case_marks_the_llm_metrics_not_applicable_when_the_text_is_null() -> None:
    # Arrange
    case = _case()
    judge = _StubJudge('{"score": 0.9, "reason": "grounded"}')
    body = _body() | {"text": None}

    # Act
    result = score_case(case, 200, body, FOREIGN, judge)

    # Assert
    metrics = _by_name(result)
    assert metrics["llm_faithfulness"].status == "not_applicable"
    assert metrics["llm_answer_relevancy"].status == "not_applicable"
    assert judge.requests == []
