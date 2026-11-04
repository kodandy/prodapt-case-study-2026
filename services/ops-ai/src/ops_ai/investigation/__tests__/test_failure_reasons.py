"""Tests for ops_ai/investigation/reasons.py, written from docs/specs/failure-tools.md section 5 (B4.2)."""

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.llm_port import LlmUnavailableError
from ops_ai.assistant.tool_calling import ToolChoice, ToolChoiceRequest, ToolPlan
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.query import FailureQuery
from ops_ai.investigation.reasons import (
    CLASSIFY_FAILURE_REASON,
    MAX_LLM_CLASSIFICATIONS,
    MIN_LLM_CONFIDENCE,
    TAXONOMY,
    UNCLASSIFIED,
    ReasonBreakdown,
    ReasonGroup,
    ReasonLabel,
    TaxonomyEntry,
    classify_reason,
    failure_reason_groups,
    label_reasons,
    transfer_reason_groups,
)
from ops_ai.scope import UserScope
from ops_ai.transfers.models import TransferFact
from sqlalchemy.orm import Session, sessionmaker

START = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
END = START + timedelta(hours=6)
MERCHANT = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OTHER_MERCHANT = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")

EXPECTED_TAXONOMY: dict[str, tuple[str, str, bool]] = {
    "issuer_unavailable": ("issuer", "issuer", False),
    "do_not_honor": ("issuer", "cardholder", False),
    "card_declined": ("issuer", "cardholder", False),
    "insufficient_funds": ("funds", "cardholder", False),
    "expired_card": ("card_data", "cardholder", False),
    "incorrect_cvc": ("card_data", "cardholder", False),
    "fraud_flagged": ("risk", "risk_team", False),
    "processing_error": ("processor", "acquirer", True),
    "rate_limited": ("processor", "platform", True),
    "cross_border_limit_kyc_tier": ("limit", "sender", False),
    "kyc_verification_failed": ("compliance", "sender", False),
    "unclassified": ("unknown", "unknown", False),
}


@dataclass
class FakeToolLlm:
    """Records every request; answers each with `plan`, or raises LlmUnavailableError when `unavailable`."""

    plan: ToolPlan = field(default_factory=lambda: ToolPlan(calls=(), model="fake"))
    unavailable: bool = False
    requests: list[ToolChoiceRequest] = field(default_factory=list)

    def choose_tools(self, request: ToolChoiceRequest) -> ToolPlan:
        self.requests.append(request)
        if self.unavailable:
            raise LlmUnavailableError("provider down")
        return self.plan


def _classification(code: str, confidence: float) -> ToolPlan:
    arguments = json.dumps({"taxonomy_code": code, "confidence": confidence})
    return ToolPlan(calls=(ToolChoice(name=CLASSIFY_FAILURE_REASON, arguments=arguments),), model="fake")


def _insert_payment(
    session: Session,
    occurred_at: datetime,
    *,
    decline_code: str | None,
    state: str = "declined",
    method: str = "card",
    currency: str = "USD",
    merchant_id: uuid.UUID = MERCHANT,
    payment_id: uuid.UUID | None = None,
) -> uuid.UUID:
    new_id = payment_id or uuid.uuid4()
    session.add(
        PaymentFact(
            payment_id=new_id,
            merchant_id=merchant_id,
            amount_minor=1500,
            currency=currency,
            method=method,
            state=state,
            provider="acq_a",
            decline_code=decline_code,
            card_bin="400000",
            entity_version=1,
            occurred_at=occurred_at,
        )
    )
    return new_id


def _insert_transfer(
    session: Session,
    decided_at: datetime,
    *,
    reason_code: str | None,
    status: str = "failed",
    sender: uuid.UUID = SENDER,
) -> uuid.UUID:
    transfer_id = uuid.uuid4()
    session.add(
        TransferFact(
            transfer_id=transfer_id,
            sender_user_id=sender,
            amount_minor=180_000,
            currency="USD",
            method="bank_transfer",
            status=status,
            country_sender="US",
            country_receiver="IN",
            reason_code=reason_code,
            rule_id="xb_limit_kyc_tier1_v3",
            decided_at=decided_at,
            ingest_source="seed",
        )
    )
    return transfer_id


BASE_QUERY = FailureQuery(
    merchant_id=MERCHANT,
    start=START,
    end=END,
    method=None,
    status=None,
    country=None,
    currency=None,
    source="rules",
    unapplied_filters=(),
)


def _query(**overrides: object) -> FailureQuery:
    return replace(BASE_QUERY, **overrides)


def _group(
    code: str,
    *,
    count: int,
    share: float,
    sample_ids: tuple[uuid.UUID, ...],
    raw_codes: tuple[str, ...],
    source: str,
) -> ReasonGroup:
    entry = TAXONOMY[code]
    return ReasonGroup(
        taxonomy_code=code,
        category=entry.category,
        owner=entry.owner,
        retryable=entry.retryable,
        count=count,
        share=share,
        sample_ids=sample_ids,
        raw_codes=raw_codes,
        source=source,
    )


# --- TAXONOMY ---------------------------------------------------------------------------------------------


def test_taxonomy_holds_exactly_the_twelve_specified_rows() -> None:
    # Arrange
    expected = {
        code: TaxonomyEntry(code=code, category=category, owner=owner, retryable=retryable)
        for code, (category, owner, retryable) in EXPECTED_TAXONOMY.items()
    }

    # Act
    actual = dict(TAXONOMY)

    # Assert
    assert actual == expected


def test_taxonomy_marks_only_processing_error_and_rate_limited_as_retryable() -> None:
    # Arrange / Act
    retryable = sorted(code for code, entry in TAXONOMY.items() if entry.retryable)

    # Assert
    assert retryable == ["processing_error", "rate_limited"]


def test_module_constants_have_the_specified_values() -> None:
    # Arrange / Act / Assert
    assert (UNCLASSIFIED, CLASSIFY_FAILURE_REASON, MIN_LLM_CONFIDENCE, MAX_LLM_CLASSIFICATIONS) == (
        "unclassified",
        "classify_failure_reason",
        0.7,
        5,
    )


# --- classify_reason --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", " ", "   ", "\t\n"])
def test_classify_reason_returns_none_for_missing_or_blank_values(raw: str | None) -> None:
    # Arrange / Act
    result = classify_reason(raw)

    # Assert
    assert result is None


@pytest.mark.parametrize("code", [code for code in EXPECTED_TAXONOMY if code != "unclassified"])
def test_classify_reason_maps_every_taxonomy_code_to_its_own_entry(code: str) -> None:
    # Arrange / Act
    result = classify_reason(code)

    # Assert
    assert result == TAXONOMY[code]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("DO NOT HONOR", "do_not_honor"),
        ("Insufficient-Funds", "insufficient_funds"),
        ("  Expired Card  ", "expired_card"),
        ("Card Declined", "card_declined"),
        ("RATE_LIMITED", "rate_limited"),
        ("processing-error", "processing_error"),
        ("Cross-Border Limit KYC Tier", "cross_border_limit_kyc_tier"),
    ],
)
def test_classify_reason_normalises_case_whitespace_spaces_and_hyphens_before_matching_codes(
    raw: str, code: str
) -> None:
    # Arrange / Act
    result = classify_reason(raw)

    # Assert
    assert result == TAXONOMY[code]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("Rate limit exceeded by client", "rate_limited"),
        ("429 Too Many Requests", "rate_limited"),
        ("Issuer unavailable for this BIN", "issuer_unavailable"),
        ("upstream timeout", "issuer_unavailable"),
        ("connection timed out", "issuer_unavailable"),
        ("Declined: do not honor", "do_not_honor"),
        ("Declined: do_not_honor", "do_not_honor"),
        ("Insufficient funds in the account", "insufficient_funds"),
        ("insufficient_funds on retry", "insufficient_funds"),
        ("The card has expired", "expired_card"),
        ("invalid CVC supplied", "incorrect_cvc"),
        ("CVV mismatch", "incorrect_cvc"),
        ("suspected fraud on the account", "fraud_flagged"),
        ("KYC verification pending", "kyc_verification_failed"),
        ("KYC check failed", "kyc_verification_failed"),
        ("cross-border limit exceeded", "cross_border_limit_kyc_tier"),
        ("KYC tier limit reached", "cross_border_limit_kyc_tier"),
        ("tier 1 limit reached", "cross_border_limit_kyc_tier"),
        ("Processing error at acquirer", "processing_error"),
        ("Payment declined by the bank", "card_declined"),
    ],
)
def test_classify_reason_applies_the_free_text_rules(raw: str, code: str) -> None:
    # Arrange / Act
    result = classify_reason(raw)

    # Assert
    assert result == TAXONOMY[code]


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("declined due to timeout", "issuer_unavailable"),
        ("card expired and declined", "expired_card"),
        ("rate limit timeout", "rate_limited"),
        ("fraud suspected, payment declined", "fraud_flagged"),
        ("kyc verification failed, limit exceeded", "kyc_verification_failed"),
        ("timeout during processing error", "issuer_unavailable"),
    ],
)
def test_classify_reason_lets_the_first_matching_rule_win(raw: str, code: str) -> None:
    # Arrange / Act
    result = classify_reason(raw)

    # Assert
    assert result == TAXONOMY[code]


@pytest.mark.parametrize(
    "raw",
    ["other", "Other", "OTHER", "unclassified", "limit reached", "kyc approved", "something odd", "42"],
)
def test_classify_reason_returns_none_for_unmapped_values_including_other_and_unclassified(raw: str) -> None:
    # Arrange / Act
    result = classify_reason(raw)

    # Assert
    assert result is None


# --- label_reasons ----------------------------------------------------------------------------------------


def test_label_reasons_labels_rule_hits_with_source_rules_and_no_confidence() -> None:
    # Arrange
    raws = ["Timed Out", "insufficient_funds"]

    # Act
    labels = label_reasons(raws, None)

    # Assert
    assert labels == {
        "Timed Out": ReasonLabel(entry=TAXONOMY["issuer_unavailable"], source="rules", confidence=None),
        "insufficient_funds": ReasonLabel(
            entry=TAXONOMY["insufficient_funds"], source="rules", confidence=None
        ),
    }


def test_label_reasons_returns_one_label_per_distinct_raw_value() -> None:
    # Arrange
    raws = ["timeout", "timeout", "timeout", "expired_card"]

    # Act
    labels = label_reasons(raws, None)

    # Assert
    assert set(labels) == {"timeout", "expired_card"}


def test_label_reasons_does_not_call_the_llm_for_rule_hits_or_blank_values() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.95))

    # Act
    label_reasons(["timeout", "expired_card", None, "", "   "], tool_llm)

    # Assert
    assert tool_llm.requests == []


def test_label_reasons_labels_missing_and_blank_values_unclassified_with_source_none() -> None:
    # Arrange
    raws: list[str | None] = [None, "", "   "]

    # Act
    labels = label_reasons(raws, FakeToolLlm(plan=_classification("processing_error", 0.95)))

    # Assert
    assert set(labels) == {None, "", "   "}
    for label in labels.values():
        assert (label.entry, label.source) == (TAXONOMY["unclassified"], "none")


def test_label_reasons_without_an_llm_leaves_unmapped_values_unclassified() -> None:
    # Arrange
    raws = ["other", "gateway glitch"]

    # Act
    labels = label_reasons(raws, None)

    # Assert
    assert {raw: (label.entry, label.source) for raw, label in labels.items()} == {
        "other": (TAXONOMY["unclassified"], "none"),
        "gateway glitch": (TAXONOMY["unclassified"], "none"),
    }


def test_label_reasons_accepts_a_confident_llm_classification_of_an_unmapped_value() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.85))

    # Act
    labels = label_reasons(["gateway glitch"], tool_llm)

    # Assert
    assert labels == {
        "gateway glitch": ReasonLabel(entry=TAXONOMY["processing_error"], source="llm", confidence=0.85)
    }


def test_label_reasons_asks_the_llm_with_one_classify_tool_and_the_raw_value_as_the_question() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.85))

    # Act
    label_reasons(["gateway glitch"], tool_llm)

    # Assert
    assert len(tool_llm.requests) == 1
    request = tool_llm.requests[0]
    assert request.question == "gateway glitch"
    assert [tool.name for tool in request.tools] == ["classify_failure_reason"]
    properties = request.tools[0].parameters["properties"]
    assert isinstance(properties, dict)
    taxonomy_code = properties["taxonomy_code"]
    confidence = properties["confidence"]
    assert isinstance(taxonomy_code, dict)
    assert isinstance(confidence, dict)
    assert sorted(taxonomy_code["enum"]) == sorted(EXPECTED_TAXONOMY)
    assert confidence["type"] == "number"
    assert (confidence["minimum"], confidence["maximum"]) == (0, 1)


def test_label_reasons_accepts_a_classification_at_exactly_the_minimum_confidence() -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_classification("rate_limited", 0.7))

    # Act
    labels = label_reasons(["gateway glitch"], tool_llm)

    # Assert
    assert labels["gateway glitch"] == ReasonLabel(
        entry=TAXONOMY["rate_limited"], source="llm", confidence=0.7
    )


@pytest.mark.parametrize("confidence", [0.0, 0.5, 0.69, 0.6999])
def test_label_reasons_treats_a_low_confidence_classification_as_unclassified(confidence: float) -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=_classification("processing_error", confidence))

    # Act
    labels = label_reasons(["gateway glitch"], tool_llm)

    # Assert
    label = labels["gateway glitch"]
    assert (label.entry, label.source) == (TAXONOMY["unclassified"], "none")


def test_label_reasons_treats_an_unavailable_llm_as_unclassified() -> None:
    # Arrange
    tool_llm = FakeToolLlm(unavailable=True)

    # Act
    labels = label_reasons(["gateway glitch", "other"], tool_llm)

    # Assert
    assert {raw: (label.entry, label.source) for raw, label in labels.items()} == {
        "gateway glitch": (TAXONOMY["unclassified"], "none"),
        "other": (TAXONOMY["unclassified"], "none"),
    }


def test_label_reasons_makes_at_most_five_llm_calls_for_distinct_values_in_first_seen_order() -> None:
    # Arrange
    raws = [f"mystery {letter}" for letter in "abcdefg"]
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.9))

    # Act
    labels = label_reasons(raws, tool_llm)

    # Assert
    assert [request.question for request in tool_llm.requests] == raws[:5]
    assert [labels[raw].source for raw in raws] == ["llm"] * 5 + ["none"] * 2
    assert [labels[raw].entry for raw in raws[5:]] == [TAXONOMY["unclassified"]] * 2


def test_label_reasons_counts_distinct_values_once_and_does_not_spend_the_cap_on_rule_hits() -> None:
    # Arrange
    unmapped = [f"mystery {letter}" for letter in "abcde"]
    raws = ["timeout", "expired_card", unmapped[0], unmapped[0], *unmapped[1:], "timeout", None]
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.9))

    # Act
    labels = label_reasons(raws, tool_llm)

    # Assert
    assert [request.question for request in tool_llm.requests] == unmapped
    assert [labels[raw].source for raw in unmapped] == ["llm"] * 5


@pytest.mark.parametrize(
    "plan",
    [
        ToolPlan(calls=(), model="fake"),
        ToolPlan(
            calls=(
                ToolChoice(CLASSIFY_FAILURE_REASON, '{"taxonomy_code": "rate_limited", "confidence": 0.9}'),
                ToolChoice(CLASSIFY_FAILURE_REASON, '{"taxonomy_code": "rate_limited", "confidence": 0.9}'),
            ),
            model="fake",
        ),
        ToolPlan(
            calls=(
                ToolChoice("submit_failure_query", '{"taxonomy_code": "rate_limited", "confidence": 0.9}'),
            ),
            model="fake",
        ),
        ToolPlan(calls=(ToolChoice(CLASSIFY_FAILURE_REASON, "not json"),), model="fake"),
        ToolPlan(calls=(ToolChoice(CLASSIFY_FAILURE_REASON, ""),), model="fake"),
        ToolPlan(calls=(ToolChoice(CLASSIFY_FAILURE_REASON, "[]"),), model="fake"),
        ToolPlan(
            calls=(ToolChoice(CLASSIFY_FAILURE_REASON, '{"taxonomy_code": "made_up", "confidence": 0.9}'),),
            model="fake",
        ),
        ToolPlan(
            calls=(
                ToolChoice(CLASSIFY_FAILURE_REASON, '{"taxonomy_code": "rate_limited", "confidence": 1.5}'),
            ),
            model="fake",
        ),
        ToolPlan(
            calls=(
                ToolChoice(
                    CLASSIFY_FAILURE_REASON, '{"taxonomy_code": "rate_limited", "confidence": "high"}'
                ),
            ),
            model="fake",
        ),
        ToolPlan(calls=(ToolChoice(CLASSIFY_FAILURE_REASON, '{"confidence": 0.9}'),), model="fake"),
    ],
    ids=[
        "no_calls",
        "two_calls",
        "wrong_name",
        "invalid_json",
        "empty_arguments",
        "json_array",
        "unknown_code",
        "confidence_above_one",
        "confidence_not_a_number",
        "missing_code",
    ],
)
def test_label_reasons_treats_invalid_llm_output_as_unclassified(plan: ToolPlan) -> None:
    # Arrange
    tool_llm = FakeToolLlm(plan=plan)

    # Act
    labels = label_reasons(["gateway glitch"], tool_llm)

    # Assert
    label = labels["gateway glitch"]
    assert (label.entry, label.source) == (TAXONOMY["unclassified"], "none")


# --- failure_reason_groups: grouping ----------------------------------------------------------------------


def test_failure_reason_groups_returns_an_empty_breakdown_for_a_window_without_failures(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_payment(session, START + timedelta(minutes=5), decline_code=None, state="succeeded")

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result == ReasonBreakdown(0, ())


def test_failure_reason_groups_groups_by_taxonomy_code_with_counts_shares_and_ordering(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        timeout_a = _insert_payment(session, START + timedelta(minutes=10), decline_code="timeout")
        timeout_b = _insert_payment(session, START + timedelta(minutes=20), decline_code="timeout")
        timeout_c = _insert_payment(session, START + timedelta(minutes=30), decline_code="timeout")
        unavailable = _insert_payment(
            session, START + timedelta(minutes=40), decline_code="issuer_unavailable"
        )
        funds_a = _insert_payment(session, START + timedelta(minutes=50), decline_code="insufficient_funds")
        funds_b = _insert_payment(session, START + timedelta(minutes=60), decline_code="insufficient_funds")
        honor = _insert_payment(session, START + timedelta(minutes=70), decline_code="do_not_honor")
        expired = _insert_payment(session, START + timedelta(minutes=80), decline_code="expired_card")

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=8,
        groups=(
            _group(
                "issuer_unavailable",
                count=4,
                share=0.5,
                sample_ids=(unavailable, timeout_c, timeout_b, timeout_a),
                raw_codes=("issuer_unavailable", "timeout"),
                source="rules",
            ),
            _group(
                "insufficient_funds",
                count=2,
                share=0.25,
                sample_ids=(funds_b, funds_a),
                raw_codes=("insufficient_funds",),
                source="rules",
            ),
            _group(
                "do_not_honor",
                count=1,
                share=0.125,
                sample_ids=(honor,),
                raw_codes=("do_not_honor",),
                source="rules",
            ),
            _group(
                "expired_card",
                count=1,
                share=0.125,
                sample_ids=(expired,),
                raw_codes=("expired_card",),
                source="rules",
            ),
        ),
    )


def test_failure_reason_groups_keeps_only_the_five_newest_sample_ids_newest_first(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        ids = [
            _insert_payment(session, START + timedelta(minutes=minute), decline_code="do_not_honor")
            for minute in range(7)
        ]

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert len(result.groups) == 1
    assert (result.groups[0].count, result.groups[0].sample_ids) == (7, tuple(reversed(ids[2:])))


def test_failure_reason_groups_breaks_sample_ties_on_equal_time_by_payment_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    low = uuid.UUID("00000000-0000-4000-8000-000000000001")
    high = uuid.UUID("ffffffff-0000-4000-8000-000000000002")
    at = START + timedelta(minutes=30)
    with session_factory.begin() as session:
        _insert_payment(session, at, decline_code="do_not_honor", payment_id=high)
        _insert_payment(session, at, decline_code="do_not_honor", payment_id=low)

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result.groups[0].sample_ids == (low, high)


def test_failure_reason_groups_puts_missing_and_unmapped_raw_values_in_one_unclassified_group(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        none_a = _insert_payment(session, START + timedelta(minutes=10), decline_code=None)
        none_b = _insert_payment(session, START + timedelta(minutes=20), decline_code=None)
        other = _insert_payment(session, START + timedelta(minutes=30), decline_code="other")

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=3,
        groups=(
            _group(
                "unclassified",
                count=3,
                share=1.0,
                sample_ids=(other, none_b, none_a),
                raw_codes=("other",),
                source="none",
            ),
        ),
    )


def test_failure_reason_groups_leaves_unmapped_values_unclassified_when_the_llm_is_unavailable(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        glitch = _insert_payment(session, START + timedelta(minutes=10), decline_code="gateway glitch")
    tool_llm = FakeToolLlm(unavailable=True)

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), tool_llm)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=1,
        groups=(
            _group(
                "unclassified",
                count=1,
                share=1.0,
                sample_ids=(glitch,),
                raw_codes=("gateway glitch",),
                source="none",
            ),
        ),
    )


def test_failure_reason_groups_uses_llm_labels_for_unmapped_values(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        glitch = _insert_payment(session, START + timedelta(minutes=10), decline_code="gateway glitch")
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.9))

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), tool_llm)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=1,
        groups=(
            _group(
                "processing_error",
                count=1,
                share=1.0,
                sample_ids=(glitch,),
                raw_codes=("gateway glitch",),
                source="llm",
            ),
        ),
    )


def test_failure_reason_groups_reports_source_rules_when_rule_and_llm_labels_share_a_group(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        by_rule = _insert_payment(
            session, START + timedelta(minutes=10), decline_code="Processing error at acquirer"
        )
        by_llm = _insert_payment(session, START + timedelta(minutes=20), decline_code="gateway glitch")
    tool_llm = FakeToolLlm(plan=_classification("processing_error", 0.9))

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), tool_llm)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=2,
        groups=(
            _group(
                "processing_error",
                count=2,
                share=1.0,
                sample_ids=(by_llm, by_rule),
                raw_codes=("Processing error at acquirer", "gateway glitch"),
                source="rules",
            ),
        ),
    )


# --- failure_reason_groups: filters -----------------------------------------------------------------------


def test_failure_reason_groups_counts_only_declined_and_failed_states(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        for state in ("succeeded", "unknown", "created"):
            _insert_payment(session, START + timedelta(minutes=10), decline_code="do_not_honor", state=state)
        declined = _insert_payment(session, START + timedelta(minutes=20), decline_code="do_not_honor")
        failed = _insert_payment(
            session, START + timedelta(minutes=30), decline_code="do_not_honor", state="failed"
        )

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result.total_failed == 2
    assert result.groups[0].sample_ids == (failed, declined)


def test_failure_reason_groups_filters_by_method(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_payment(session, START + timedelta(minutes=10), decline_code="do_not_honor", method="card")
        wallet = _insert_payment(
            session, START + timedelta(minutes=20), decline_code="do_not_honor", method="wallet"
        )

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(method="wallet"), None)

    # Assert
    assert result.total_failed == 1
    assert result.groups[0].sample_ids == (wallet,)


@pytest.mark.parametrize("status", ["declined", "failed"])
def test_failure_reason_groups_filters_by_a_single_failed_state(
    session_factory: sessionmaker[Session], status: str
) -> None:
    # Arrange
    with session_factory.begin() as session:
        declined = _insert_payment(
            session, START + timedelta(minutes=10), decline_code="do_not_honor", state="declined"
        )
        failed = _insert_payment(
            session, START + timedelta(minutes=20), decline_code="do_not_honor", state="failed"
        )
    expected = declined if status == "declined" else failed

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(status=status), None)

    # Assert
    assert result.total_failed == 1
    assert result.groups[0].sample_ids == (expected,)


def test_failure_reason_groups_filters_by_currency(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_payment(session, START + timedelta(minutes=10), decline_code="do_not_honor", currency="USD")
        eur = _insert_payment(
            session, START + timedelta(minutes=20), decline_code="do_not_honor", currency="EUR"
        )

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(currency="EUR"), None)

    # Assert
    assert result.total_failed == 1
    assert result.groups[0].sample_ids == (eur,)


def test_failure_reason_groups_does_not_filter_by_country(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_payment(session, START + timedelta(minutes=10), decline_code="do_not_honor")

    # Act
    with session_factory() as session:
        result = failure_reason_groups(
            session, _query(country="IN", unapplied_filters=("country",)), None
        )

    # Assert
    assert result.total_failed == 1


def test_failure_reason_groups_includes_the_start_instant_and_excludes_the_end_instant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_payment(session, START - timedelta(seconds=1), decline_code="do_not_honor")
        at_start = _insert_payment(session, START, decline_code="do_not_honor")
        before_end = _insert_payment(session, END - timedelta(seconds=1), decline_code="do_not_honor")
        _insert_payment(session, END, decline_code="do_not_honor")

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result.total_failed == 2
    assert result.groups[0].sample_ids == (before_end, at_start)


def test_failure_reason_groups_excludes_other_merchants(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    with session_factory.begin() as session:
        mine = _insert_payment(session, START + timedelta(minutes=10), decline_code="do_not_honor")
        _insert_payment(
            session, START + timedelta(minutes=20), decline_code="do_not_honor", merchant_id=OTHER_MERCHANT
        )

    # Act
    with session_factory() as session:
        result = failure_reason_groups(session, _query(), None)

    # Assert
    assert result.total_failed == 1
    assert result.groups[0].sample_ids == (mine,)


# --- transfer_reason_groups -------------------------------------------------------------------------------


def test_transfer_reason_groups_returns_an_empty_breakdown_without_failed_transfers(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_transfer(session, START + timedelta(minutes=10), reason_code=None, status="completed")

    # Act
    with session_factory() as session:
        result = transfer_reason_groups(session, UserScope(user_id=SENDER), START, END, None)

    # Assert
    assert result == ReasonBreakdown(0, ())


def test_transfer_reason_groups_groups_the_failed_transfers_of_the_user_by_taxonomy_code(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        limit_a = _insert_transfer(
            session, START + timedelta(minutes=10), reason_code="cross_border_limit_kyc_tier"
        )
        limit_b = _insert_transfer(
            session, START + timedelta(minutes=20), reason_code="cross_border_limit_kyc_tier"
        )
        limit_c = _insert_transfer(
            session, START + timedelta(minutes=30), reason_code="cross_border_limit_kyc_tier"
        )
        kyc = _insert_transfer(session, START + timedelta(minutes=40), reason_code="kyc_verification_failed")

    # Act
    with session_factory() as session:
        result = transfer_reason_groups(session, UserScope(user_id=SENDER), START, END, None)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=4,
        groups=(
            _group(
                "cross_border_limit_kyc_tier",
                count=3,
                share=0.75,
                sample_ids=(limit_c, limit_b, limit_a),
                raw_codes=("cross_border_limit_kyc_tier",),
                source="rules",
            ),
            _group(
                "kyc_verification_failed",
                count=1,
                share=0.25,
                sample_ids=(kyc,),
                raw_codes=("kyc_verification_failed",),
                source="rules",
            ),
        ),
    )


def test_transfer_reason_groups_excludes_other_users_and_non_failed_transfers(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        mine = _insert_transfer(
            session, START + timedelta(minutes=10), reason_code="cross_border_limit_kyc_tier"
        )
        code = "cross_border_limit_kyc_tier"
        _insert_transfer(session, START + timedelta(minutes=20), reason_code=code, sender=OTHER_SENDER)
        _insert_transfer(session, START + timedelta(minutes=30), reason_code=code, status="completed")
        _insert_transfer(session, START + timedelta(minutes=40), reason_code=code, status="flagged")

    # Act
    with session_factory() as session:
        result = transfer_reason_groups(session, UserScope(user_id=SENDER), START, END, None)

    # Assert
    assert result.total_failed == 1
    assert result.groups[0].sample_ids == (mine,)


def test_transfer_reason_groups_includes_the_start_instant_and_excludes_the_end_instant(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        _insert_transfer(session, START - timedelta(seconds=1), reason_code="kyc_verification_failed")
        at_start = _insert_transfer(session, START, reason_code="kyc_verification_failed")
        _insert_transfer(session, END, reason_code="kyc_verification_failed")

    # Act
    with session_factory() as session:
        result = transfer_reason_groups(session, UserScope(user_id=SENDER), START, END, None)

    # Assert
    assert result.total_failed == 1
    assert result.groups[0].sample_ids == (at_start,)


def test_transfer_reason_groups_labels_a_missing_reason_code_unclassified(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    with session_factory.begin() as session:
        transfer_id = _insert_transfer(session, START + timedelta(minutes=10), reason_code=None)

    # Act
    with session_factory() as session:
        result = transfer_reason_groups(session, UserScope(user_id=SENDER), START, END, None)

    # Assert
    assert result == ReasonBreakdown(
        total_failed=1,
        groups=(
            _group(
                "unclassified",
                count=1,
                share=1.0,
                sample_ids=(transfer_id,),
                raw_codes=(),
                source="none",
            ),
        ),
    )
