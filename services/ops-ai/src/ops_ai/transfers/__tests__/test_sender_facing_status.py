"""Tests for the sender-facing status, written from docs/specs/sq2.md section 9.1 (audit M2)."""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from ops_ai import transfers
from ops_ai.assistant.llm_port import LlmRequest, LlmResponse
from ops_ai.scope import UserScope
from ops_ai.transfers import TransferStatus, classify_transfer, get_payment_status
from ops_ai.transfers.service import TransferInvestigation, investigate_transfer
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

SENDER = uuid.UUID("a1a1a1a1-0000-4000-8000-000000000001")
SCOPE = UserScope(user_id=SENDER)
DECIDED_AT = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
QUESTION = "My international transfer was held even though I had enough money - what happened?"
NARRATION = "narration from the recording llm"
MODEL = "recording-model-1"
FORBIDDEN_WORDS = ("flagged", "fraud", "review")
# A rule id that would leak the reason for the hold if it ever reached the sender.
LEAKY_RULE_ID = "fraud_model_review_v2"

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
        "status": "flagged",
        "country_sender": "US",
        "country_receiver": "IN",
        "reason_code": None,
        "rule_id": LEAKY_RULE_ID,
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
    session_factory: sessionmaker[Session], transfer_id: uuid.UUID, llm: RecordingLlm
) -> TransferInvestigation:
    with session_factory() as session:
        result = investigate_transfer(session, SCOPE, QUESTION, transfer_id, llm)
    assert isinstance(result, TransferInvestigation)
    return result


def _status(status: str) -> TransferStatus:
    return TransferStatus(
        transfer_id=uuid.UUID("5f1b7c2e-8d4a-4e6b-9a3c-0d2e4f6a8b10"),
        amount_minor=180_000,
        currency="USD",
        method="bank_transfer",
        status=status,
        country_sender="US",
        country_receiver="IN",
        reason_code="cross_border_limit_kyc_tier",
        kyc_tier=1,
        limit_minor=100_000,
        limit_currency="USD",
        next_tier_limit_minor=1_000_000,
        sender_balance_minor=525_000,
        decided_at=DECIDED_AT,
    )


# --- the mapping table -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("success", "success"),
        ("failed", "failed"),
        ("pending", "pending"),
        ("reversed", "reversed"),
        ("flagged", "pending"),
    ],
)
def test_sender_facing_status_maps_the_known_raw_values(raw: str, expected: str) -> None:
    # Arrange / Act
    result = transfers.sender_facing_status(raw)

    # Assert
    assert result == expected


@pytest.mark.parametrize(
    "raw",
    ["", "FLAGGED", "Success", " failed", "failed ", "refunded", "fraud_hold", "on_hold", "unknown"],
)
def test_sender_facing_status_maps_any_other_raw_value_to_pending(raw: str) -> None:
    # Arrange / Act
    result = transfers.sender_facing_status(raw)

    # Assert
    assert result == "pending"


# --- the classifier and the tool keep the raw status -----------------------------------------------------


def test_a_flagged_transfer_still_classifies_as_not_declined_on_the_raw_status() -> None:
    # Arrange
    transfer = _status("flagged")

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("not_declined", "unknown")


def test_a_failed_transfer_still_classifies_on_the_raw_status() -> None:
    # Arrange
    transfer = _status("failed")

    # Act
    result = classify_transfer(transfer)

    # Assert
    assert result == ("cross_border_limit_for_kyc_tier", "sender")


def test_get_payment_status_keeps_the_raw_flagged_status(session_factory: sessionmaker[Session]) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    with session_factory() as session:
        transfer = get_payment_status(session, SCOPE, transfer_id)

    # Assert
    assert transfer is not None
    assert transfer.status == "flagged"


def test_investigate_transfer_classifies_a_flagged_transfer_as_not_declined(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _investigate(session_factory, transfer_id, RecordingLlm())

    # Assert
    assert (result.cause_code, result.remediation_owner) == ("not_declined", "unknown")
    assert result.suggested_fixes == ()


# --- the evidence and the LLM request use the mapped status ----------------------------------------------


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("flagged", "pending"),
        ("success", "success"),
        ("failed", "failed"),
        ("reversed", "reversed"),
        ("pending", "pending"),
        ("on_hold", "pending"),
    ],
)
def test_the_transfer_evidence_summary_uses_the_sender_facing_status(
    session_factory: sessionmaker[Session], raw: str, shown: str
) -> None:
    # Arrange
    transfer_id = _insert(session_factory, status=raw)

    # Act
    result = _investigate(session_factory, transfer_id, RecordingLlm())

    # Assert
    summaries = {item.ref: item.summary for item in result.evidence}
    assert summaries[f"transfer:{transfer_id}"] == (
        f"Transfer of 1800.00 USD from US to IN, {shown} at {result.transfer.decided_at.isoformat()}"
    )


def test_a_flagged_transfer_leaks_none_of_the_forbidden_words_into_the_evidence_the_fixes_or_the_text(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)

    # Act
    result = _investigate(session_factory, transfer_id, RecordingLlm())

    # Assert
    shown = " ".join(
        [item.ref + " " + item.summary for item in result.evidence]
        + list(result.suggested_fixes)
        + [result.text or ""]
    ).lower()
    assert [word for word in FORBIDDEN_WORDS if word in shown] == []


def test_a_flagged_transfer_leaks_none_of_the_forbidden_words_into_the_llm_request(
    session_factory: sessionmaker[Session],
) -> None:
    # Arrange
    transfer_id = _insert(session_factory)
    llm = RecordingLlm()

    # Act
    _investigate(session_factory, transfer_id, llm)

    # Assert
    assert len(llm.calls) == 1
    request_text = repr(llm.calls[0]).lower()
    assert [word for word in FORBIDDEN_WORDS if word in request_text] == []
    assert "pending at" in request_text
