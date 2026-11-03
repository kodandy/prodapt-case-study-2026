import dataclasses
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import UserScope
from ops_ai.transfers.tools import (
    CauseCode,
    RemediationOwner,
    TransferStatus,
    classify_transfer,
    get_payment_status,
)

NEXT_KYC_TIER = 2


@dataclass(frozen=True)
class TransferInvestigation:
    cause_code: CauseCode
    remediation_owner: RemediationOwner
    transfer: TransferStatus
    balance_covered_amount: bool | None
    """The balance at decision time was at least the amount; says nothing about fees.
    None without a balance."""
    suggested_fixes: tuple[str, ...]
    tool_calls: tuple[ToolCall, ...]
    evidence: tuple[EvidenceItem, ...]
    mode: AnswerMode
    text: str | None
    """None in t2_fallback."""
    model: str | None


def investigate_transfer(
    session: Session, scope: UserScope, question: str, transfer_id: uuid.UUID | None, llm: LlmPort
) -> TransferInvestigation | None:
    """Looks up the sender's own transfer (the newest failed one without an id), names the cause only when the
    record supports it, and lets the LLM narrate the evidence (LlmUnavailableError → t2_fallback with the same
    fields). None when the scope has no such transfer; the LLM is then never called.
    """
    tool_call = ToolCall(
        "get_payment_status", {"transfer_id": "latest_failed" if transfer_id is None else str(transfer_id)}
    )
    transfer = get_payment_status(session, scope, transfer_id)
    if transfer is None:
        return None
    cause_code, owner = classify_transfer(transfer)
    balance = transfer.sender_balance_minor
    evidence = _evidence(transfer, cause_code)
    analysis = TransferInvestigation(
        cause_code=cause_code,
        remediation_owner=owner,
        transfer=transfer,
        balance_covered_amount=None if balance is None else balance >= transfer.amount_minor,
        suggested_fixes=_suggested_fixes(transfer, cause_code),
        tool_calls=(tool_call,),
        evidence=evidence,
        mode="t2_fallback",
        text=None,
        model=None,
    )
    try:
        response = llm.complete(LlmRequest(question=question[:MAX_QUESTION_CHARS], evidence=evidence))
    except LlmUnavailableError:
        return analysis
    return dataclasses.replace(analysis, mode="llm", text=response.text, model=response.model)


def _money(minor: int, currency: str) -> str:
    """Two-decimal currencies only; every currency the fixture produces is one."""
    return f"{minor // 100}.{minor % 100:02d} {currency}"


def _suggested_fixes(transfer: TransferStatus, cause_code: CauseCode) -> tuple[str, ...]:
    if cause_code == "cross_border_limit_for_kyc_tier":
        next_limit, limit_currency = transfer.next_tier_limit_minor, transfer.limit_currency
        if next_limit is None or limit_currency is None or next_limit < transfer.amount_minor:
            return ()
        return (
            f"Upgrade to KYC tier {NEXT_KYC_TIER}: its cross-border limit of "
            f"{_money(next_limit, limit_currency)} per transfer covers "
            f"{_money(transfer.amount_minor, transfer.currency)}.",
        )
    if cause_code == "insufficient_funds":
        return ("Add funds to your balance so that it covers the transfer amount, then send it again.",)
    if cause_code == "kyc_verification_failed":
        return ("Complete identity verification, then send the transfer again.",)
    if cause_code == "unclassified":
        return (f"Contact support with reference {transfer.transfer_id}.",)
    return ()


def _evidence(transfer: TransferStatus, cause_code: CauseCode) -> tuple[EvidenceItem, ...]:
    transfer_id = transfer.transfer_id
    amount = _money(transfer.amount_minor, transfer.currency)
    balance = transfer.sender_balance_minor
    balance_text = "unknown" if balance is None else _money(balance, transfer.currency)
    items = [
        EvidenceItem(
            ref=f"transfer:{transfer_id}",
            summary=(
                f"Transfer of {amount} from {transfer.country_sender} to {transfer.country_receiver}, "
                f"{transfer.status} at {transfer.decided_at.isoformat()}"
            ),
        ),
        EvidenceItem(
            ref=f"balance:{transfer_id}",
            summary=f"Sender balance at decision time {balance_text} vs amount {amount}",
        ),
    ]
    if (
        cause_code == "cross_border_limit_for_kyc_tier"
        and transfer.limit_minor is not None
        and transfer.limit_currency is not None
    ):
        items.append(
            EvidenceItem(
                ref=f"limit:{transfer_id}",
                summary=(
                    f"Cross-border per-transfer limit for KYC tier {transfer.kyc_tier} "
                    f"{_money(transfer.limit_minor, transfer.limit_currency)} vs amount {amount}"
                ),
            )
        )
    return tuple(items)
