"""Tests for investigate_transfer, written from docs/specs/sq2.md sections 5 and 6 (hand-built data)."""

import dataclasses
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse, LlmUnavailableError
from ops_ai.scope import UserScope
from ops_ai.transfers import TransferStatus
from ops_ai.transfers.service import TransferInvestigation, investigate_transfer
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
OTHER_SENDER = uuid.UUID("b2b2b2b2-0000-4000-8000-000000000002")
SCOPE = UserScope(user_id=SENDER)
DECIDED_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
XB_RULE = "xb_limit_kyc_tier1_v3"
KYC_RULE = "kyc_gate_v1"
QUESTION = "My international transfer was declined even though I had enough money - what triggered it?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"

INSERT = text(
    "INSERT INTO ops_ai.transfer_facts (transfer_id, sender_user_id, amount_minor, currency, method, status, "
    "country_sender, country_receiver, reason_code, rule_id, kyc_tier, limit_minor, limit_currency, "
    "next_tier_limit_minor, sender_balance_minor, decided_at, ingest_source) VALUES "
    "(:transfer_id, :sender_user_id, :amount_minor, :currency, :method, :status, :country_sender, "
    ":country_receiver, :reason_code, :rule_id, :kyc_tier, :limit_minor, :limit_currency, "
    ":next_tier_limit_minor, :sender_balance_minor, :decided_at, :ingest_source)"
)

NO_LIMIT: dict[str, object] = {
    "kyc_tier": None,
    "limit_minor": None,
    "limit_currency": None,
    "next_tier_limit_minor": None,
}
CASES: dict[str, dict[str, object]] = {
    "cross_border_limit": {},
    "insufficient_funds": {
        "reason_code": "insufficient_funds",
        "rule_id": None,
        "sender_balance_minor": 40_000,
        "country_receiver": "US",
        **NO_LIMIT,
    },
    "kyc_verification_failed": {"reason_code": "kyc_verification_failed", "rule_id": KYC_RULE, **NO_LIMIT},
    "unclassified_provider_error": {"reason_code": "provider_error", "rule_id": None, **NO_LIMIT},
    "limit_but_next_tier_too_low": {
        "amount_minor": 1_500_000,
        "sender_balance_minor": 2_000_000,
        "next_tier_limit_minor": 1_000_000,
    },
}
EXPECTED_CAUSES = {
    "cross_border_limit": ("cross_border_limit_for_kyc_tier", "sender"),
    "insufficient_funds": ("insufficient_funds", "sender"),
    "kyc_verification_failed": ("kyc_verification_failed", "sender"),
    "unclassified_provider_error": ("unclassified", "unknown"),
    "limit_but_next_tier_too_low": ("cross_border_limit_for_kyc_tier", "sender"),
}


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


@dataclass
class DownLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        raise LlmUnavailableError("provider outage")


def _insert(session_factory: sessionmaker[Session], **overrides: object) -> uuid.UUID:
    params: dict[str, object] = {
        "transfer_id": uuid.uuid4(),
        "sender_user_id": SENDER,
        "amount_minor": 180_000,
        "currency": "USD",
        "method": "bank_transfer",
        "status": "failed",
        "country_sender": "US",
        "country_receiver": "IN",
        "reason_code": "cross_border_limit_kyc_tier",
        "rule_id": XB_RULE,
        "kyc_tier": 1,
        "limit_minor": 100_000,
        "limit_currency": "USD",
        "next_tier_limit_minor": 1_000_000,
        "sender_balance_minor": 525_000,
        "decided_at": DECIDED_AT,
        "ingest_source": "seed",
    }
    params.update(overrides)
    with session_factory.begin() as session:
        session.execute(INSERT, params)
    transfer_id = params["transfer_id"]
    assert isinstance(transfer_id, uuid.UUID)
    return transfer_id


def _investigate(
    session_factory: sessionmaker[Session],
    transfer_id: uuid.UUID | None,
    llm: RecordingLlm | DownLlm | None = None,
    scope: UserScope = SCOPE,
) -> TransferInvestigation | None:
    with session_factory() as session:
        return investigate_transfer(session, scope, QUESTION, transfer_id, llm or RecordingLlm())


def _found(
    session_factory: sessionmaker[Session],
    transfer_id: uuid.UUID | None,
    llm: RecordingLlm | DownLlm | None = None,
) -> TransferInvestigation:
    result = _investigate(session_factory, transfer_id, llm)
    assert isinstance(result, TransferInvestigation)
    return result


def _summaries(result: TransferInvestigation) -> dict[str, str]:
    return {item.ref: item.summary for item in result.evidence}


# --- the tool call and the result fields -----------------------------------------------------------------


def test_investigate_transfer_calls_get_payment_status_with_the_given_transfer_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert [(call.name, call.args) for call in result.tool_calls] == [
        ("get_payment_status", {"transfer_id": str(transfer_id)})
    ]


def test_investigate_transfer_without_an_id_calls_the_tool_with_latest_failed(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory)

    # Act
    result = _found(session_factory, None)

    # Assert
    assert [(call.name, call.args) for call in result.tool_calls] == [
        ("get_payment_status", {"transfer_id": "latest_failed"})
    ]


def test_investigate_transfer_names_the_cause_the_owner_and_carries_the_transfer_without_a_rule_id(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert (result.cause_code, result.remediation_owner) == ("cross_border_limit_for_kyc_tier", "sender")
    assert result.transfer == TransferStatus(
        transfer_id,
        180_000,
        "USD",
        "bank_transfer",
        "failed",
        "US",
        "IN",
        "cross_border_limit_kyc_tier",
        1,
        100_000,
        "USD",
        1_000_000,
        525_000,
        DECIDED_AT,
    )


def test_investigate_transfer_without_an_id_investigates_the_newest_failed_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory, decided_at=datetime(2026, 11, 3, 7, 0, tzinfo=UTC))
    newest = _insert(session_factory, decided_at=datetime(2026, 11, 3, 9, 30, tzinfo=UTC))
    _insert(
        session_factory, status="success", reason_code=None, decided_at=datetime(2026, 11, 3, 10, tzinfo=UTC)
    )

    # Act
    result = _found(session_factory, None)

    # Assert
    assert result.transfer.transfer_id == newest


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_investigate_transfer_classifies_every_case(
    session_factory: sessionmaker[Session], case: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES[case])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert (result.cause_code, result.remediation_owner) == EXPECTED_CAUSES[case]


def test_investigate_transfer_result_is_a_frozen_dataclass(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    result = _found(session_factory, _insert(session_factory))

    # Act
    with pytest.raises(dataclasses.FrozenInstanceError) as exc:
        result.cause_code = "unclassified"  # type: ignore[misc]

    # Assert
    assert exc.type is dataclasses.FrozenInstanceError


# --- balance_covered_amount ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("balance", "expected"),
    [(525_000, True), (180_000, True), (179_999, False), (0, False), (None, None)],
    ids=["well_above", "equal", "one_short", "zero", "unknown"],
)
def test_balance_covered_amount_compares_the_balance_with_the_amount(
    session_factory: sessionmaker[Session], balance: int | None, expected: bool | None
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, sender_balance_minor=balance)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert result.balance_covered_amount is expected


# --- suggested fixes -------------------------------------------------------------------------------------


def test_the_limit_case_suggests_one_fix_naming_kyc_tier_2_and_its_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert len(result.suggested_fixes) == 1
    fix = result.suggested_fixes[0].lower()
    assert "tier 2" in fix
    assert "10000.00 usd" in fix


def test_the_limit_case_suggests_the_upgrade_when_the_next_tier_limit_equals_the_amount(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(
        session_factory,
        amount_minor=1_000_000,
        sender_balance_minor=2_000_000,
        next_tier_limit_minor=1_000_000,
    )

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert result.cause_code == "cross_border_limit_for_kyc_tier"
    assert len(result.suggested_fixes) == 1
    assert "tier 2" in result.suggested_fixes[0].lower()


def test_the_limit_case_suggests_no_fix_when_the_next_tier_limit_is_below_the_amount(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES["limit_but_next_tier_too_low"])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert result.cause_code == "cross_border_limit_for_kyc_tier"
    assert result.suggested_fixes == ()


def test_the_limit_case_suggests_no_fix_when_the_next_tier_limit_is_unknown(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, next_tier_limit_minor=None)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert result.suggested_fixes == ()


def test_the_insufficient_funds_case_suggests_adding_funds(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES["insufficient_funds"])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert len(result.suggested_fixes) == 1
    assert "add funds" in result.suggested_fixes[0].lower()


def test_the_kyc_failure_case_suggests_completing_identity_verification(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES["kyc_verification_failed"])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert len(result.suggested_fixes) == 1
    assert "identity verification" in result.suggested_fixes[0].lower()


def test_the_unclassified_case_suggests_contacting_support_with_the_transfer_reference(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES["unclassified_provider_error"])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert len(result.suggested_fixes) == 1
    assert result.suggested_fixes[0].lower().startswith("contact support with reference")
    assert str(transfer_id) in result.suggested_fixes[0]


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_nothing_the_sender_is_told_suggests_splitting_the_transfer(
    session_factory: sessionmaker[Session], case: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES[case])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    told = " ".join([*result.suggested_fixes, *(f"{e.ref} {e.summary}" for e in result.evidence)]).lower()
    assert "split" not in told
    assert "structur" not in told


# --- evidence --------------------------------------------------------------------------------------------


def test_the_limit_case_cites_the_transfer_the_balance_and_the_limit_in_that_order(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert [item.ref for item in result.evidence] == [
        f"transfer:{transfer_id}",
        f"balance:{transfer_id}",
        f"limit:{transfer_id}",
    ]


@pytest.mark.parametrize(
    "case", ["insufficient_funds", "kyc_verification_failed", "unclassified_provider_error"]
)
def test_a_case_without_a_limit_cites_only_the_transfer_and_the_balance(
    session_factory: sessionmaker[Session], case: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES[case])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    assert [item.ref for item in result.evidence] == [f"transfer:{transfer_id}", f"balance:{transfer_id}"]


def test_the_transfer_evidence_has_the_amount_with_its_currency_the_corridor_and_the_decision_time(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    summary = _summaries(result)[f"transfer:{transfer_id}"]
    assert "1800.00 USD" in summary
    assert "US" in summary
    assert "IN" in summary
    assert "2026-11-03" in summary


def test_amounts_are_formatted_in_major_units_with_two_decimals_and_the_currency(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, amount_minor=180_050, sender_balance_minor=525_007)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    summaries = _summaries(result)
    assert "1800.50 USD" in summaries[f"transfer:{transfer_id}"]
    assert "5250.07 USD" in summaries[f"balance:{transfer_id}"]
    assert "1800.50 USD" in summaries[f"balance:{transfer_id}"]


def test_the_balance_evidence_sets_the_balance_against_the_amount(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    summary = _summaries(result)[f"balance:{transfer_id}"]
    assert "5250.00 USD" in summary
    assert "1800.00 USD" in summary


def test_the_limit_evidence_names_the_kyc_tier_and_sets_its_limit_against_the_amount(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    summary = _summaries(result)[f"limit:{transfer_id}"]
    assert "tier 1" in summary.lower()
    assert "1000.00 USD" in summary
    assert "1800.00 USD" in summary


def test_the_insufficient_funds_balance_evidence_shows_the_short_balance(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES["insufficient_funds"])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    summary = _summaries(result)[f"balance:{transfer_id}"]
    assert "400.00 USD" in summary
    assert "1800.00 USD" in summary


@pytest.mark.parametrize("case", list(CASES), ids=list(CASES))
def test_the_rule_id_never_reaches_the_evidence_the_fixes_or_the_result(
    session_factory: sessionmaker[Session], case: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, **CASES[case])

    # Act
    result = _found(session_factory, transfer_id)

    # Assert
    visible = repr(result) + " ".join(f"{item.ref} {item.summary}" for item in result.evidence)
    assert XB_RULE not in visible
    assert KYC_RULE not in visible
    assert "xb_limit" not in visible
    assert "kyc_gate" not in visible


# --- the LLM only narrates the evidence ------------------------------------------------------------------


def test_the_llm_receives_the_question_and_exactly_the_evidence_and_its_narration_is_returned(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    llm = RecordingLlm()

    # Act
    result = _found(session_factory, transfer_id, llm)

    # Assert
    assert len(llm.calls) == 1
    assert llm.calls[0].question == QUESTION
    assert llm.calls[0].evidence == result.evidence
    assert (result.mode, result.text, result.model) == ("llm", NARRATION, MODEL)


def test_the_llm_never_sees_the_rule_id(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    llm = RecordingLlm()

    # Act
    _found(session_factory, transfer_id, llm)

    # Assert
    seen = repr(llm.calls)
    assert XB_RULE not in seen
    assert "xb_limit" not in seen


def test_an_unavailable_llm_gives_t2_fallback_with_the_same_fields_and_no_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    with_llm = _found(session_factory, transfer_id, RecordingLlm())
    down = DownLlm()

    # Act
    fallback = _found(session_factory, transfer_id, down)

    # Assert
    assert (fallback.mode, fallback.text, fallback.model) == ("t2_fallback", None, None)
    assert len(down.calls) == 1
    assert dataclasses.replace(fallback, mode="llm", text=NARRATION, model=MODEL) == with_llm


# --- not found for this sender ---------------------------------------------------------------------------


def test_investigate_transfer_returns_none_for_another_senders_transfer_and_never_calls_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    others_transfer = _insert(session_factory, sender_user_id=OTHER_SENDER)
    llm = RecordingLlm()

    # Act
    result = _investigate(session_factory, others_transfer, llm)

    # Assert
    assert result is None
    assert llm.calls == []


def test_investigate_transfer_returns_none_for_a_missing_transfer_id_and_never_calls_the_llm(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory)
    llm = RecordingLlm()

    # Act
    result = _investigate(session_factory, uuid.UUID("deadbeef-0000-4000-8000-000000000000"), llm)

    # Assert
    assert result is None
    assert llm.calls == []


def test_investigate_transfer_without_an_id_returns_none_when_the_sender_has_no_failed_transfer(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    _insert(session_factory, status="success", reason_code=None)
    _insert(session_factory, sender_user_id=OTHER_SENDER)
    llm = RecordingLlm()

    # Act
    result = _investigate(session_factory, None, llm)

    # Assert
    assert result is None
    assert llm.calls == []


def test_investigate_transfer_is_scoped_to_the_sender_of_the_scope(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    own = _insert(session_factory, amount_minor=180_000)
    others = _insert(
        session_factory, sender_user_id=OTHER_SENDER, amount_minor=777_700, sender_balance_minor=9_000_000
    )

    # Act
    mine = _found(session_factory, None)
    theirs = _investigate(session_factory, None, scope=UserScope(user_id=OTHER_SENDER))

    # Assert
    assert mine.transfer.transfer_id == own
    assert theirs is not None
    assert theirs.transfer.transfer_id == others
    assert str(others) not in repr(mine)
