from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from ops_ai.search.embedder import Embedder, LocalHashEmbedder
from ops_ai.search.service import hybrid_search

AnswerMode = Literal["llm", "t2_fallback"]
MAX_QUESTION_CHARS = 2000
SEARCH_LIMIT = 10


@dataclass(frozen=True)
class AssistantAnswer:
    mode: AnswerMode
    text: str | None
    """None in t2_fallback: only the evidence list is shown."""
    evidence: tuple[EvidenceItem, ...]
    model: str | None


def ask(
    session: Session, scope: Scope, question: str, llm: LlmPort, embedder: Embedder | None = None
) -> AssistantAnswer:
    """Collects evidence (failure counts by decline_code from payment_facts + hybrid search hits for the
    question), all within scope, then asks the LLM. Any LlmUnavailableError → mode t2_fallback with the same
    evidence. `embedder` None → LocalHashEmbedder; an unavailable embedder degrades search to keyword only.
    """
    bounded = question[:MAX_QUESTION_CHARS]
    search_embedder = embedder if embedder is not None else LocalHashEmbedder()
    evidence = (
        *_decline_aggregates(session, scope),
        *_search_evidence(session, scope, bounded, search_embedder),
    )
    session.close()
    try:
        response = llm.complete(LlmRequest(question=bounded, evidence=evidence))
    except LlmUnavailableError:
        return AssistantAnswer(mode="t2_fallback", text=None, evidence=evidence, model=None)
    return AssistantAnswer(mode="llm", text=response.text, evidence=evidence, model=response.model)


def _decline_aggregates(session: Session, scope: Scope) -> list[EvidenceItem]:
    rows = session.execute(
        select(PaymentFact.decline_code, func.count().label("count"))
        .where(
            PaymentFact.merchant_id == scope.merchant_id,
            PaymentFact.state == "declined",
            PaymentFact.decline_code.is_not(None),
        )
        .group_by(PaymentFact.decline_code)
        .order_by(func.count().desc(), PaymentFact.decline_code)
    ).all()
    return [
        EvidenceItem(
            ref=f"aggregate:decline_code:{row.decline_code}",
            summary=f"{row.count} declined payments with decline code {row.decline_code}",
        )
        for row in rows
    ]


def _search_evidence(session: Session, scope: Scope, question: str, embedder: Embedder) -> list[EvidenceItem]:
    if not question.strip():
        return []
    hits = hybrid_search(session, scope, question, embedder, limit=SEARCH_LIMIT)
    return [EvidenceItem(ref=f"{hit.kind}:{hit.ref_id}", summary=f"{hit.title}: {hit.body}") for hit in hits]
