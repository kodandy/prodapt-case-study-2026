"""B4.2: raw decline / reason codes become taxonomy groups. Rules first, the LLM only for unmapped text."""

import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ops_ai.assistant.llm_port import LlmUnavailableError
from ops_ai.assistant.tool_calling import ToolCallingLlm, ToolChoiceRequest, ToolSpec
from ops_ai.ingest.models import PaymentFact
from ops_ai.investigation.query import FailureQuery
from ops_ai.investigation.tools import FAILED_STATES
from ops_ai.scope import UserScope
from ops_ai.transfers.models import TransferFact

UNCLASSIFIED = "unclassified"
CLASSIFY_FAILURE_REASON = "classify_failure_reason"
MIN_LLM_CONFIDENCE = 0.7
MAX_LLM_CLASSIFICATIONS = 5
MAX_SAMPLE_IDS = 5
TRANSFER_FAILED_STATUS = "failed"

TaxonomyCode = Literal[
    "issuer_unavailable",
    "do_not_honor",
    "card_declined",
    "insufficient_funds",
    "expired_card",
    "incorrect_cvc",
    "fraud_flagged",
    "processing_error",
    "rate_limited",
    "cross_border_limit_kyc_tier",
    "kyc_verification_failed",
    "unclassified",
]
LabelSource = Literal["rules", "llm", "none"]


@dataclass(frozen=True)
class TaxonomyEntry:
    code: str
    category: str
    owner: str
    retryable: bool
    """True only where a retry cannot double-charge: a timed-out charge has an unknown outcome."""


def _entry(code: str, category: str, owner: str, retryable: bool = False) -> tuple[str, TaxonomyEntry]:
    return code, TaxonomyEntry(code=code, category=category, owner=owner, retryable=retryable)


TAXONOMY: Mapping[str, TaxonomyEntry] = MappingProxyType(
    dict(
        (
            _entry("issuer_unavailable", "issuer", "issuer"),
            _entry("do_not_honor", "issuer", "cardholder"),
            _entry("card_declined", "issuer", "cardholder"),
            _entry("insufficient_funds", "funds", "cardholder"),
            _entry("expired_card", "card_data", "cardholder"),
            _entry("incorrect_cvc", "card_data", "cardholder"),
            _entry("fraud_flagged", "risk", "risk_team"),
            _entry("processing_error", "processor", "acquirer", retryable=True),
            _entry("rate_limited", "processor", "platform", retryable=True),
            _entry("cross_border_limit_kyc_tier", "limit", "sender"),
            _entry("kyc_verification_failed", "compliance", "sender"),
            _entry(UNCLASSIFIED, "unknown", "unknown"),
        )
    )
)

_SOURCE_RANK: Mapping[str, int] = MappingProxyType({"rules": 0, "llm": 1, "none": 2})


def _free_text_code(text: str) -> str | None:
    def has(*needles: str) -> bool:
        return any(needle in text for needle in needles)

    if has("rate limit", "too many requests"):
        return "rate_limited"
    if has("issuer unavailable", "timeout", "timed out"):
        return "issuer_unavailable"
    if has("do not honor", "do_not_honor"):
        return "do_not_honor"
    if has("insufficient funds", "insufficient_funds"):
        return "insufficient_funds"
    if has("expired"):
        return "expired_card"
    if has("cvc", "cvv"):
        return "incorrect_cvc"
    if has("fraud"):
        return "fraud_flagged"
    if has("kyc") and has("fail", "verif"):
        return "kyc_verification_failed"
    if has("limit") and has("cross", "kyc", "tier"):
        return "cross_border_limit_kyc_tier"
    if has("processing error"):
        return "processing_error"
    if has("declined"):
        return "card_declined"
    return None


def classify_reason(raw: str | None) -> TaxonomyEntry | None:
    """Rules only. None means unmapped."""
    if raw is None or not raw.strip():
        return None
    text = raw.strip().lower()
    normalised = text.replace(" ", "_").replace("-", "_")
    if normalised != UNCLASSIFIED and normalised in TAXONOMY:
        return TAXONOMY[normalised]
    code = _free_text_code(text)
    return TAXONOMY[code] if code is not None else None


@dataclass(frozen=True)
class ReasonLabel:
    entry: TaxonomyEntry
    source: LabelSource
    confidence: float | None


class ClassifyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    taxonomy_code: TaxonomyCode
    confidence: float = Field(ge=0, le=1)


_CLASSIFY_INSTRUCTIONS = (
    "Classify the payment failure reason into one taxonomy code by calling classify_failure_reason once. "
    "Use unclassified when unsure, and give your confidence between 0 and 1."
)
_UNCLASSIFIED_LABEL = ReasonLabel(entry=TAXONOMY[UNCLASSIFIED], source="none", confidence=None)


def classify_failure_reason_spec() -> ToolSpec:
    return ToolSpec(
        name=CLASSIFY_FAILURE_REASON,
        description="Classify one payment failure reason into the platform taxonomy.",
        parameters=ClassifyArgs.model_json_schema(),
    )


def _llm_label(raw: str, tool_llm: ToolCallingLlm) -> ReasonLabel:
    request = ToolChoiceRequest(
        instructions=_CLASSIFY_INSTRUCTIONS, question=raw, tools=(classify_failure_reason_spec(),)
    )
    try:
        plan = tool_llm.choose_tools(request)
    except LlmUnavailableError:
        return _UNCLASSIFIED_LABEL
    if len(plan.calls) != 1 or plan.calls[0].name != CLASSIFY_FAILURE_REASON:
        return _UNCLASSIFIED_LABEL
    try:
        payload: object = json.loads(plan.calls[0].arguments)
    except ValueError:
        return _UNCLASSIFIED_LABEL
    if not isinstance(payload, dict):
        return _UNCLASSIFIED_LABEL
    try:
        args = ClassifyArgs.model_validate(payload)
    except ValidationError:
        return _UNCLASSIFIED_LABEL
    if args.confidence < MIN_LLM_CONFIDENCE or args.taxonomy_code == UNCLASSIFIED:
        return _UNCLASSIFIED_LABEL
    return ReasonLabel(entry=TAXONOMY[args.taxonomy_code], source="llm", confidence=args.confidence)


def label_reasons(
    raws: Iterable[str | None], tool_llm: ToolCallingLlm | None
) -> dict[str | None, ReasonLabel]:
    labels: dict[str | None, ReasonLabel] = {}
    llm_calls = 0
    for raw in raws:
        if raw in labels:
            continue
        if raw is None or not raw.strip():
            labels[raw] = _UNCLASSIFIED_LABEL
            continue
        entry = classify_reason(raw)
        if entry is not None:
            labels[raw] = ReasonLabel(entry=entry, source="rules", confidence=None)
        elif tool_llm is not None and llm_calls < MAX_LLM_CLASSIFICATIONS:
            llm_calls += 1
            labels[raw] = _llm_label(raw, tool_llm)
        else:
            labels[raw] = _UNCLASSIFIED_LABEL
    return labels


@dataclass(frozen=True)
class ReasonGroup:
    taxonomy_code: str
    category: str
    owner: str
    retryable: bool
    count: int
    share: float
    sample_ids: tuple[uuid.UUID, ...]
    """Up to 5 newest ids, newest first."""
    raw_codes: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class ReasonBreakdown:
    total_failed: int
    groups: tuple[ReasonGroup, ...]


def _group_rows(rows: list[tuple[uuid.UUID, str | None]], tool_llm: ToolCallingLlm | None) -> ReasonBreakdown:
    """`rows` are (id, raw value), newest first."""
    if not rows:
        return ReasonBreakdown(0, ())
    labels = label_reasons((raw for _, raw in rows), tool_llm)
    members: dict[str, list[tuple[uuid.UUID, str | None]]] = {}
    for row in rows:
        members.setdefault(labels[row[1]].entry.code, []).append(row)
    total = len(rows)
    groups: list[ReasonGroup] = []
    for code, group_rows in members.items():
        entry = TAXONOMY[code]
        raws = {raw for _, raw in group_rows}
        source = min((labels[raw].source for raw in raws), key=lambda value: _SOURCE_RANK[value])
        groups.append(
            ReasonGroup(
                taxonomy_code=code,
                category=entry.category,
                owner=entry.owner,
                retryable=entry.retryable,
                count=len(group_rows),
                share=len(group_rows) / total,
                sample_ids=tuple(row_id for row_id, _ in group_rows[:MAX_SAMPLE_IDS]),
                raw_codes=tuple(sorted(raw for raw in raws if raw is not None)),
                source=source,
            )
        )
    groups.sort(key=lambda group: (-group.count, group.taxonomy_code))
    return ReasonBreakdown(total_failed=total, groups=tuple(groups))


def _release_before_llm(session: Session, tool_llm: ToolCallingLlm | None) -> None:
    """No connection may sit idle in a transaction while an LLM HTTP call runs."""
    if tool_llm is not None:
        session.rollback()


def failure_reason_groups(
    session: Session, query: FailureQuery, tool_llm: ToolCallingLlm | None
) -> ReasonBreakdown:
    states = (query.status,) if query.status is not None else FAILED_STATES
    statement = select(PaymentFact.payment_id, PaymentFact.decline_code).where(
        PaymentFact.merchant_id == query.merchant_id,
        PaymentFact.state.in_(states),
        PaymentFact.occurred_at >= query.start,
        PaymentFact.occurred_at < query.end,
    )
    if query.method is not None:
        statement = statement.where(PaymentFact.method == query.method)
    if query.currency is not None:
        statement = statement.where(PaymentFact.currency == query.currency)
    statement = statement.order_by(PaymentFact.occurred_at.desc(), PaymentFact.payment_id)
    rows = [(row.payment_id, row.decline_code) for row in session.execute(statement)]
    _release_before_llm(session, tool_llm)
    return _group_rows(rows, tool_llm)


def transfer_reason_groups(
    session: Session, scope: UserScope, start: datetime, end: datetime, tool_llm: ToolCallingLlm | None
) -> ReasonBreakdown:
    statement = (
        select(TransferFact.transfer_id, TransferFact.reason_code)
        .where(
            TransferFact.sender_user_id == scope.user_id,
            TransferFact.status == TRANSFER_FAILED_STATUS,
            TransferFact.decided_at >= start,
            TransferFact.decided_at < end,
        )
        .order_by(TransferFact.decided_at.desc(), TransferFact.transfer_id)
    )
    rows = [(row.transfer_id, row.reason_code) for row in session.execute(statement)]
    _release_before_llm(session, tool_llm)
    return _group_rows(rows, tool_llm)
