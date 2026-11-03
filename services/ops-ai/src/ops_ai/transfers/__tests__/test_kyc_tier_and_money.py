"""Tests for the KYC tier text, the missing tier and money formatting, from docs/specs/sq2.md section 9.4."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.scope import UserScope
from ops_ai.transfers import TransferStatus, classify_transfer
from ops_ai.transfers import service as transfer_service
from ops_ai.transfers.service import TransferInvestigation, _money, investigate_transfer
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
SCOPE = UserScope(user_id=SENDER)
TRANSFER_ID = uuid.UUID("5f1b7c2e-8d4a-4e6b-9a3c-0d2e4f6a8b10")
DECIDED_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
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


@dataclass
class RecordingLlm:
    calls: list[LlmRequest] = field(default_factory=list)

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.calls.append(request)
        return LlmResponse(text=NARRATION, model=MODEL)


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
        "rule_id": "xb_limit_kyc_tier1_v3",
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


def _investigate(session_factory: sessionmaker[Session], transfer_id: uuid.UUID) -> TransferInvestigation:
    with session_factory() as session:
        result = investigate_transfer(session, SCOPE, QUESTION, transfer_id, RecordingLlm())
    assert isinstance(result, TransferInvestigation)
    return result


def _status(*, kyc_tier: int | None) -> TransferStatus:
    return TransferStatus(
        transfer_id=TRANSFER_ID,
        amount_minor=180_000,
        currency="USD",
        method="bank_transfer",
        status="failed",
        country_sender="US",
        country_receiver="IN",
        reason_code="cross_border_limit_kyc_tier",
        kyc_tier=kyc_tier,
        limit_minor=100_000,
        limit_currency="USD",
        next_tier_limit_minor=1_000_000,
        sender_balance_minor=525_000,
        decided_at=DECIDED_AT,
    )


# --- the fix names kyc_tier + 1 --------------------------------------------------------------------------


def test_the_fix_for_a_tier_2_record_names_kyc_tier_3_and_its_limit(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(
        session_factory,
        kyc_tier=2,
        amount_minor=1_800_000,
        limit_minor=1_000_000,
        next_tier_limit_minor=5_000_000,
        sender_balance_minor=2_500_000,
    )

    # Act
    result = _investigate(session_factory, transfer_id)

    # Assert
    assert (result.cause_code, result.remediation_owner) == ("cross_border_limit_for_kyc_tier", "sender")
    assert len(result.suggested_fixes) == 1
    fix = result.suggested_fixes[0]
    assert "KYC tier 3" in fix
    assert "KYC tier 2" not in fix
    assert "50000.00 USD" in fix
    assert "18000.00 USD" in fix


def test_the_fix_for_a_tier_1_record_still_names_kyc_tier_2(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _investigate(session_factory, transfer_id)

    # Assert
    assert len(result.suggested_fixes) == 1
    fix = result.suggested_fixes[0]
    assert "KYC tier 2" in fix
    assert "KYC tier 3" not in fix


def test_the_limit_evidence_names_the_current_tier_not_the_next_one(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(
        session_factory,
        kyc_tier=2,
        amount_minor=1_800_000,
        limit_minor=1_000_000,
        next_tier_limit_minor=5_000_000,
        sender_balance_minor=2_500_000,
    )

    # Act
    result = _investigate(session_factory, transfer_id)

    # Assert
    summaries = {item.ref: item.summary for item in result.evidence}
    assert summaries[f"limit:{transfer_id}"] == (
        "Cross-border per-transfer limit for KYC tier 2 10000.00 USD vs amount 18000.00 USD"
    )


def test_the_fixed_next_kyc_tier_constant_is_gone() -> None:
    # Arrange / Act
    has_constant = hasattr(transfer_service, "NEXT_KYC_TIER")

    # Assert
    assert has_constant is False


# --- no tier: unclassified -------------------------------------------------------------------------------


def test_classify_transfer_without_a_kyc_tier_is_unclassified_even_when_the_numbers_match() -> None:
    # Arrange
    transfer = _status(kyc_tier=None)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("unclassified", "unknown")


def test_classify_transfer_with_a_kyc_tier_still_names_the_limit_cause() -> None:
    # Arrange
    transfer = _status(kyc_tier=1)

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("cross_border_limit_for_kyc_tier", "sender")


def test_investigate_transfer_without_a_kyc_tier_is_unclassified_with_a_support_fix_and_no_limit_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, kyc_tier=None)

    # Act
    result = _investigate(session_factory, transfer_id)

    # Assert
    assert (result.cause_code, result.remediation_owner) == ("unclassified", "unknown")
    assert result.suggested_fixes == (f"Contact support with reference {transfer_id}.",)
    assert [item.ref for item in result.evidence] == [f"transfer:{transfer_id}", f"balance:{transfer_id}"]


# --- _money ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minor", "expected"),
    [
        (-150, "-1.50 USD"),
        (-5, "-0.05 USD"),
        (-100, "-1.00 USD"),
        (-1, "-0.01 USD"),
        (-123_456, "-1234.56 USD"),
        (0, "0.00 USD"),
        (5, "0.05 USD"),
        (150, "1.50 USD"),
        (180_000, "1800.00 USD"),
    ],
)
def test_money_formats_minor_units_with_the_sign_in_front(minor: int, expected: str) -> None:
    # Arrange / Act
    result = _money(minor, "USD")

    # Assert
    assert result == expected


def test_a_negative_sender_balance_is_shown_with_the_sign_in_front_in_the_balance_evidence(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(
        session_factory,
        reason_code="insufficient_funds",
        rule_id=None,
        country_receiver="US",
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
        sender_balance_minor=-150,
    )

    # Act
    result = _investigate(session_factory, transfer_id)

    # Assert
    summaries = {item.ref: item.summary for item in result.evidence}
    assert (
        summaries[f"balance:{transfer_id}"]
        == "Sender balance at decision time -1.50 USD vs amount 1800.00 USD"
    )
    assert result.cause_code == "insufficient_funds"
    assert result.balance_covered_amount is False
