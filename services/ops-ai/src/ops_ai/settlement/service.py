import dataclasses
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.assistant.service import MAX_QUESTION_CHARS, AnswerMode
from ops_ai.investigation.service import ToolCall
from ops_ai.scope import Scope
from ops_ai.settlement.contract import FX_SNAPSHOT, STANDARD_CONTRACT
from ops_ai.settlement.statement import SettlementLine, SettlementStatement
from ops_ai.settlement.tools import explain_settlement

NARRATED_SKIP = ("gross", "mdr")
LINE_LABELS = {
    "fixed_fees": "Fixed fees",
    "fx": "FX conversion",
    "refunds": "Refunds",
    "chargebacks": "Chargebacks",
    "chargeback_fees": "Chargeback fees",
    "reserve": "Rolling reserve",
}


@dataclass(frozen=True)
class SettlementExplanation:
    statement: SettlementStatement
    summary: str
    """Deterministic, from the verified lines only; never LLM output."""
    tool_calls: tuple[ToolCall, ...]
    evidence: tuple[EvidenceItem, ...]
    mode: AnswerMode
    text: str | None
    """None in t2_fallback."""
    model: str | None


def explain_payout(
    session: Session, scope: Scope, question: str, period: str | None, llm: LlmPort
) -> SettlementExplanation | None:
    """Tools first: explain_settlement builds and verifies the statement; only a verified one goes to the LLM
    (LlmUnavailableError → t2_fallback with the same fields). None when there is no statement for the period;
    the LLM is then never called.
    """
    tool_call = ToolCall("explain_settlement", {"period": period or "latest"})
    statement = explain_settlement(session, scope, period)
    if statement is None:
        return None
    if not statement.verified:
        return SettlementExplanation(
            statement=statement,
            summary=(
                f"The {statement.period} settlement could not be verified "
                f"({', '.join(statement.mismatches)}); contact support with reference {statement.period}."
            ),
            tool_calls=(tool_call,),
            evidence=(_statement_evidence(statement), _crosscheck_evidence(statement)),
            mode="t2_fallback",
            text=None,
            model=None,
        )
    evidence = _verified_evidence(statement)
    explanation = SettlementExplanation(
        statement=statement,
        summary=_summary(statement),
        tool_calls=(tool_call,),
        evidence=evidence,
        mode="t2_fallback",
        text=None,
        model=None,
    )
    session.close()
    try:
        response = llm.complete(LlmRequest(question=question[:MAX_QUESTION_CHARS], evidence=evidence))
    except LlmUnavailableError:
        return explanation
    return dataclasses.replace(explanation, mode="llm", text=response.text, model=response.model)


def _money(minor: int, currency: str) -> str:
    """The absolute amount with two decimals; the settlement currency has two."""
    units, cents = divmod(abs(minor), 100)
    return f"{units}.{cents:02d} {currency}"


def _percent(bps: int) -> str:
    whole, hundredths = divmod(bps, 100)
    return f"{whole}.{hundredths:02d}%"


def _narrated(statement: SettlementStatement) -> list[SettlementLine]:
    return [line for line in statement.lines if line.kind not in NARRATED_SKIP and line.amount_minor != 0]


def _summary(statement: SettlementStatement) -> str:
    currency = statement.currency
    net = _money(statement.net_minor, currency)
    gross = _money(statement.gross_minor, currency)
    mdr = _percent(STANDARD_CONTRACT.mdr_bps)
    if statement.net_minor < 0:
        expected = _money(statement.expected_minor, currency)
        opening = (
            f"Your {statement.period} settlement has no payout: you owe {net}, "
            f"{_money(statement.gap_minor, currency)} less than the {expected} "
            f"you would expect from gross sales of {gross} minus the {mdr} MDR."
        )
    elif statement.gap_minor == 0:
        opening = (
            f"Your {statement.period} payout is {net}, the same as expected from gross sales of {gross} "
            f"minus the {mdr} MDR."
        )
    else:
        opening = (
            f"Your {statement.period} payout is {net}, {_money(statement.gap_minor, currency)} less than the "
            f"{_money(statement.expected_minor, currency)} you would expect from gross sales of {gross} "
            f"minus the {mdr} MDR."
        )
    return " ".join([opening, *(_line_sentence(line, statement) for line in _narrated(statement))])


def _line_sentence(line: SettlementLine, statement: SettlementStatement) -> str:
    currency = statement.currency
    amount = _money(line.amount_minor, currency)
    n = line.count
    if line.kind == "fixed_fees":
        fee = _money(STANDARD_CONTRACT.fixed_fee_minor, currency)
        return f"Fixed fees: {amount} ({n} payments at {fee} each)."
    if line.kind == "fx":
        return f"FX conversion: {amount} on {n} non-{currency} payments (rate set {statement.fx_rate_id})."
    if line.kind == "refunds":
        return f"Refunds: {amount} on {n} payments."
    if line.kind == "chargebacks":
        return f"Chargebacks: {amount} on {n} payments."
    if line.kind == "chargeback_fees":
        fee = _money(STANDARD_CONTRACT.chargeback_fee_minor, currency)
        return f"Chargeback fees: {amount} ({n} at {fee} each)."
    return (
        f"Rolling reserve: {amount} held ({_percent(STANDARD_CONTRACT.reserve_bps)} of gross), "
        "released in a later payout."
    )


def _statement_evidence(statement: SettlementStatement) -> EvidenceItem:
    currency = statement.currency
    return EvidenceItem(
        ref=f"statement:{statement.period}",
        summary=(
            f"Settlement statement {statement.period} in {currency}: "
            f"gross {_money(statement.gross_minor, currency)}, "
            f"expected after MDR {_money(statement.expected_minor, currency)}, "
            f"{'net payout' if statement.net_minor >= 0 else 'net balance owed by you'} "
            f"{_money(statement.net_minor, currency)}, gap {_money(statement.gap_minor, currency)}"
        ),
    )


def _line_evidence(line: SettlementLine, currency: str) -> EvidenceItem:
    return EvidenceItem(
        ref=f"line:{line.kind}",
        summary=(
            f"{LINE_LABELS[line.kind]} deducted: {_money(line.amount_minor, currency)} "
            f"over {line.count} payments"
        ),
    )


def _contract_evidence(statement: SettlementStatement) -> EvidenceItem:
    contract = STANDARD_CONTRACT
    currency = statement.currency
    return EvidenceItem(
        ref=f"contract:{statement.contract_version}",
        summary=(
            f"Contract {statement.contract_version}: MDR {_percent(contract.mdr_bps)} of gross, fixed fee "
            f"{_money(contract.fixed_fee_minor, currency)} per payment, chargeback fee "
            f"{_money(contract.chargeback_fee_minor, currency)} per chargeback, FX markup "
            f"{_percent(contract.fx_markup_bps)} on non-{currency} payments, rolling reserve "
            f"{_percent(contract.reserve_bps)} of gross"
        ),
    )


def _fx_evidence(statement: SettlementStatement) -> EvidenceItem:
    rates = ", ".join(
        f"{code} {rate}"
        for code, rate in sorted(FX_SNAPSHOT.usd_per_unit.items())
        if code != statement.currency
    )
    return EvidenceItem(
        ref=f"fx:{statement.fx_rate_id}",
        summary=f"FX rate set {statement.fx_rate_id}, {statement.currency} per unit: {rates}",
    )


def _crosscheck_evidence(statement: SettlementStatement) -> EvidenceItem:
    if statement.verified:
        summary = (
            f"{statement.projection_checked} payments of {statement.period} matched the payment projection "
            "(same amount, currency and card method, succeeded); refund and chargeback flags come from the "
            "settlement report only"
        )
    else:
        summary = (
            f"{statement.projection_checked} payments of {statement.period} checked; the statement could not "
            f"be verified: {', '.join(statement.mismatches)}"
        )
    return EvidenceItem(ref=f"crosscheck:{statement.period}", summary=summary)


def _verified_evidence(statement: SettlementStatement) -> tuple[EvidenceItem, ...]:
    narrated = _narrated(statement)
    fx_items = [_fx_evidence(statement)] if any(line.kind == "fx" for line in narrated) else []
    return (
        _statement_evidence(statement),
        *(_line_evidence(line, statement.currency) for line in narrated),
        _contract_evidence(statement),
        *fx_items,
        _crosscheck_evidence(statement),
    )
