"""Tests for scripts/transfer_fixture.py, written from docs/specs/sq2.md section 3 (Transfer fixture)."""

import dataclasses
import uuid
from datetime import UTC, datetime

import pytest
from scripts.transfer_fixture import KYC_TIER_LIMITS_MINOR, TransferDecision, transfer_decision

from scripts.generate_dataset import (
    CanonicalTransaction,
    PaymentMethod,
    RecordKind,
    ScenarioLog,
    Status,
    generate,
)

TRANSFER_ID = uuid.UUID("5f1b7c2e-8d4a-4e6b-9a3c-0d2e4f6a8b10")
XB_RULE = "xb_limit_kyc_tier1_v3"
KYC_RULE = "kyc_gate_v1"
XB_REASON = "Cross-border per-transaction limit exceeded for KYC tier 1 (1800.00 USD > 1000.00 USD)"
KYC_REASON = "Transfer blocked: sender KYC verification failed"


def _tx(
    *,
    record_kind: RecordKind = "p2p_transfer",
    status: Status = "failed",
    failure_reason: str | None = None,
    rule_id: str | None = None,
    sender_balance_minor: int | None = 525_000,
    amount_minor: int = 180_000,
    currency: str = "USD",
    payment_method: PaymentMethod = "bank_transfer",
    merchant_id: str | None = None,
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=TRANSFER_ID,
        user_id=uuid.UUID("a1a1a1a1-0000-4000-8000-00000000000a"),
        merchant_id=merchant_id,
        amount_minor=amount_minor,
        currency=currency,
        payment_method=payment_method,
        status=status,
        failure_reason=failure_reason,
        fraud_score=0.1,
        timestamp=datetime(2026, 10, 31, 10, 42, 7, tzinfo=UTC),
        chargeback_flag=False,
        settlement_status="pending",
        resolution_notes=None,
        kyc_status="verified",
        country_sender="US",
        country_receiver="IN",
        record_kind=record_kind,
        scenario="baseline",
        card_bin=None,
        acquirer=None,
        rule_id=rule_id,
        sender_balance_minor=sender_balance_minor,
    )


def _decision(tx: CanonicalTransaction) -> TransferDecision:
    decision = transfer_decision(tx)
    assert isinstance(decision, TransferDecision)
    return decision


@pytest.fixture(scope="module")
def log() -> ScenarioLog:
    return generate(seed=42)


# --- constants and shape ---------------------------------------------------------------------------------


def test_kyc_tier_limits_are_100_000_for_tier_1_and_1_000_000_for_tier_2_in_usd_minor_units() -> None:
    # Arrange / Act
    limits = KYC_TIER_LIMITS_MINOR

    # Assert
    assert limits == {1: 100_000, 2: 1_000_000}


def test_transfer_decision_is_a_frozen_dataclass_with_the_specified_fields_in_order() -> None:
    # Arrange
    decision = _decision(_tx(failure_reason="Insufficient balance"))

    # Act
    names = [field.name for field in dataclasses.fields(TransferDecision)]

    # Assert
    assert names == [
        "transfer_id",
        "reason_code",
        "rule_id",
        "kyc_tier",
        "limit_minor",
        "limit_currency",
        "next_tier_limit_minor",
        "sender_balance_minor",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError) as exc:
        decision.kyc_tier = 2  # type: ignore[misc]
    assert exc.type is dataclasses.FrozenInstanceError


# --- only p2p transfers have a decision record ------------------------------------------------------------


@pytest.mark.parametrize("status", ["success", "failed", "pending", "flagged", "reversed"])
def test_a_merchant_payment_has_no_transfer_decision(status: Status) -> None:
    # Arrange
    tx = _tx(
        record_kind="merchant_payment",
        merchant_id="mch_0004",
        payment_method="card",
        status=status,
        failure_reason="Insufficient funds on card" if status == "failed" else None,
    )

    # Act
    decision = transfer_decision(tx)

    # Assert
    assert decision is None


def test_a_merchant_payment_carrying_a_transfer_rule_id_still_has_no_transfer_decision() -> None:
    # Arrange
    tx = _tx(record_kind="merchant_payment", merchant_id="mch_0004", payment_method="card", rule_id=XB_RULE)

    # Act
    decision = transfer_decision(tx)

    # Assert
    assert decision is None


# --- failure mapping -------------------------------------------------------------------------------------


def test_the_cross_border_rule_maps_to_the_tier_1_limit_with_the_next_tier_limit() -> None:
    # Arrange
    tx = _tx(failure_reason=XB_REASON, rule_id=XB_RULE)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision == TransferDecision(
        transfer_id=TRANSFER_ID,
        reason_code="cross_border_limit_kyc_tier",
        rule_id=XB_RULE,
        kyc_tier=1,
        limit_minor=100_000,
        limit_currency="USD",
        next_tier_limit_minor=1_000_000,
        sender_balance_minor=525_000,
    )


def test_the_kyc_gate_rule_maps_to_kyc_verification_failed_with_no_limit_fields() -> None:
    # Arrange
    tx = _tx(failure_reason=KYC_REASON, rule_id=KYC_RULE)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision == TransferDecision(
        transfer_id=TRANSFER_ID,
        reason_code="kyc_verification_failed",
        rule_id=KYC_RULE,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
        sender_balance_minor=525_000,
    )


def test_insufficient_balance_maps_to_insufficient_funds_with_no_limit_fields() -> None:
    # Arrange
    tx = _tx(failure_reason="Insufficient balance", sender_balance_minor=40_000)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision == TransferDecision(
        transfer_id=TRANSFER_ID,
        reason_code="insufficient_funds",
        rule_id=None,
        kyc_tier=None,
        limit_minor=None,
        limit_currency=None,
        next_tier_limit_minor=None,
        sender_balance_minor=40_000,
    )


@pytest.mark.parametrize("reason", ["Beneficiary account closed", "Invalid beneficiary details"])
def test_beneficiary_failures_map_to_beneficiary_rejected(reason: str) -> None:
    # Arrange
    tx = _tx(failure_reason=reason)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision.reason_code == "beneficiary_rejected"
    assert (decision.kyc_tier, decision.limit_minor, decision.limit_currency) == (None, None, None)
    assert decision.next_tier_limit_minor is None


@pytest.mark.parametrize(
    "reason",
    [
        "Wallet balance too low",
        "UPI PIN incorrect",
        "Remitter bank unavailable",
        "Some reason nobody has seen",
        "",
        None,
    ],
    ids=["wallet_low", "upi_pin", "remitter_bank", "unknown_text", "empty_text", "no_reason"],
)
def test_any_other_failed_transfer_maps_to_provider_error(reason: str | None) -> None:
    # Arrange
    tx = _tx(failure_reason=reason)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision.reason_code == "provider_error"
    assert decision.rule_id is None
    assert (decision.kyc_tier, decision.limit_minor, decision.limit_currency) == (None, None, None)
    assert decision.next_tier_limit_minor is None


def test_the_rule_id_is_checked_before_the_failure_reason() -> None:
    # Arrange
    tx = _tx(failure_reason="Insufficient balance", rule_id=XB_RULE)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision.reason_code == "cross_border_limit_kyc_tier"
    assert decision.limit_minor == 100_000


@pytest.mark.parametrize("status", ["success", "pending", "reversed"])
def test_a_transfer_that_did_not_fail_has_a_decision_with_no_reason_code(status: Status) -> None:
    # Arrange
    tx = _tx(status=status, failure_reason=None, rule_id=None)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision.reason_code is None
    assert decision.transfer_id == TRANSFER_ID


# --- balance copy ----------------------------------------------------------------------------------------


def test_the_sender_balance_is_copied_from_the_record_in_its_currency() -> None:
    # Arrange
    tx = _tx(failure_reason=XB_REASON, rule_id=XB_RULE, sender_balance_minor=777_001, currency="EUR")

    # Act
    decision = _decision(tx)

    # Assert
    assert decision.sender_balance_minor == 777_001


def test_a_missing_sender_balance_stays_missing() -> None:
    # Arrange
    tx = _tx(failure_reason="Insufficient balance", sender_balance_minor=None)

    # Act
    decision = _decision(tx)

    # Assert
    assert decision.sender_balance_minor is None
    assert decision.reason_code == "insufficient_funds"


# --- the generated dataset -------------------------------------------------------------------------------


def test_the_planted_sq2_record_maps_to_the_tier_1_cross_border_limit(log: ScenarioLog) -> None:
    # Arrange
    tx = next(t for t in log.transactions if t.transaction_id == log.planted.sq2_transaction_id)

    # Act
    decision = _decision(tx)

    # Assert
    assert tx.amount_minor == 180_000
    assert decision == TransferDecision(
        transfer_id=log.planted.sq2_transaction_id,
        reason_code="cross_border_limit_kyc_tier",
        rule_id=XB_RULE,
        kyc_tier=1,
        limit_minor=100_000,
        limit_currency="USD",
        next_tier_limit_minor=1_000_000,
        sender_balance_minor=525_000,
    )


def test_every_p2p_record_of_the_dataset_has_a_decision_and_every_other_record_has_none(
    log: ScenarioLog,
) -> None:
    # Arrange
    p2p = [tx for tx in log.transactions if tx.record_kind == "p2p_transfer"]
    others = [tx for tx in log.transactions if tx.record_kind != "p2p_transfer"]

    # Act
    p2p_decisions = [transfer_decision(tx) for tx in p2p]
    other_decisions = [transfer_decision(tx) for tx in others]

    # Assert
    assert len(p2p) > 100
    assert [d for d in p2p_decisions if d is None] == []
    assert [d for d in other_decisions if d is not None] == []


def test_the_planted_record_is_the_only_cross_border_limit_decision_of_the_dataset(
    log: ScenarioLog,
) -> None:
    # Arrange
    decisions = [d for d in map(transfer_decision, log.transactions) if d is not None]

    # Act
    limit_cases = [d.transfer_id for d in decisions if d.reason_code == "cross_border_limit_kyc_tier"]

    # Assert
    assert limit_cases == [log.planted.sq2_transaction_id]


def test_dataset_decisions_follow_the_record_for_every_failed_and_non_failed_p2p_transfer(
    log: ScenarioLog,
) -> None:
    # Arrange
    p2p = [tx for tx in log.transactions if tx.record_kind == "p2p_transfer"]

    # Act
    observed = {tx.transaction_id: _decision(tx) for tx in p2p}

    # Assert
    for tx in p2p:
        decision = observed[tx.transaction_id]
        assert decision.transfer_id == tx.transaction_id
        assert decision.sender_balance_minor == tx.sender_balance_minor
        if tx.status != "failed":
            assert decision.reason_code is None
        elif tx.rule_id == KYC_RULE:
            assert decision.reason_code == "kyc_verification_failed"
        elif tx.rule_id == XB_RULE:
            assert decision.reason_code == "cross_border_limit_kyc_tier"
        elif tx.failure_reason == "Insufficient balance":
            assert decision.reason_code == "insufficient_funds"
        elif tx.failure_reason in ("Beneficiary account closed", "Invalid beneficiary details"):
            assert decision.reason_code == "beneficiary_rejected"
        else:
            assert decision.reason_code == "provider_error"
