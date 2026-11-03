"""Stand-in for the external transfer system's decision record (docs/specs/sq2.md section 3).

The platform does not execute transfers or make KYC decisions; this maps a canonical p2p record to the
structured decision the external system would report.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from scripts.generate_dataset import CanonicalTransaction

TransferReasonCode = Literal[
    "cross_border_limit_kyc_tier",
    "insufficient_funds",
    "kyc_verification_failed",
    "beneficiary_rejected",
    "provider_error",
]

KYC_TIER_LIMITS_MINOR = {1: 100_000, 2: 1_000_000}
"""Cross-border per-transaction limit by KYC tier, USD minor units."""

CROSS_BORDER_TIER_1_RULE_ID = "xb_limit_kyc_tier1_v3"
KYC_GATE_RULE_ID = "kyc_gate_v1"
KYC_LIMIT_CURRENCY = "USD"
BENEFICIARY_FAILURES = ("Beneficiary account closed", "Invalid beneficiary details")


@dataclass(frozen=True)
class TransferDecision:
    transfer_id: uuid.UUID
    reason_code: TransferReasonCode | None
    rule_id: str | None
    kyc_tier: int | None
    limit_minor: int | None
    limit_currency: str | None
    next_tier_limit_minor: int | None
    sender_balance_minor: int | None


def _reason_code(tx: CanonicalTransaction) -> TransferReasonCode | None:
    if tx.status != "failed":
        return None
    if tx.rule_id == CROSS_BORDER_TIER_1_RULE_ID:
        return "cross_border_limit_kyc_tier"
    if tx.rule_id == KYC_GATE_RULE_ID:
        return "kyc_verification_failed"
    if tx.failure_reason == "Insufficient balance":
        return "insufficient_funds"
    if tx.failure_reason in BENEFICIARY_FAILURES:
        return "beneficiary_rejected"
    return "provider_error"


def transfer_decision(tx: CanonicalTransaction) -> TransferDecision | None:
    """The decision record for a p2p transfer; None for any other record kind."""
    if tx.record_kind != "p2p_transfer":
        return None
    reason_code = _reason_code(tx)
    rule_id = (
        tx.rule_id if reason_code in ("cross_border_limit_kyc_tier", "kyc_verification_failed") else None
    )
    is_limit = reason_code == "cross_border_limit_kyc_tier"
    return TransferDecision(
        transfer_id=tx.transaction_id,
        reason_code=reason_code,
        rule_id=rule_id,
        kyc_tier=1 if is_limit else None,
        limit_minor=KYC_TIER_LIMITS_MINOR[1] if is_limit else None,
        limit_currency=KYC_LIMIT_CURRENCY if is_limit else None,
        next_tier_limit_minor=KYC_TIER_LIMITS_MINOR[2] if is_limit else None,
        sender_balance_minor=tx.sender_balance_minor,
    )
