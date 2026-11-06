from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ops_ai.assistant.citations import Grounding, cannot_answer, ground_answer, relevant_evidence
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.ingest.models import PaymentFact
from ops_ai.scope import Scope
from ops_ai.search.embedder import Embedder, EmbedderUnavailableError, LocalHashEmbedder
from ops_ai.search.service import (
    DEFAULT_MIN_SIMILARITY,
    MIN_LEG_CANDIDATES,
    RRF_K,
    SearchHit,
    hybrid_search,
    reciprocal_rank_fusion,
    search_documents,
    vector_search,
)

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


@dataclass(frozen=True)
class GroundedAssistantAnswer:
    answer: AssistantAnswer
    """`text` is the grounded text: supported sentences only, or CANNOT_ANSWER_TEXT; None in t2_fallback."""
    grounding: Grounding | None
    """None in t2_fallback (no LLM text to check)."""
    relevant_refs: tuple[str, ...]


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
    return [_hit_evidence(hit) for hit in hits]


def ask_grounded(
    session: Session, scope: Scope, question: str, llm: LlmPort, embedder: Embedder | None = None
) -> GroundedAssistantAnswer:
    """R1-5: the same evidence as `ask` (one hybrid search: keyword + vector legs fused with RRF), then only
    the relevant evidence (citations.relevant_evidence: vector similarity >= DEFAULT_MIN_SIMILARITY, or a
    shared question term) goes to the LLM with require_citations. No relevant evidence → CANNOT_ANSWER_TEXT,
    grounded=False, mode "llm", and the LLM is not called. The LLM text is then checked sentence by sentence
    (citations.ground_answer). LlmUnavailableError → t2_fallback with no text and no grounding.
    """
    bounded = question[:MAX_QUESTION_CHARS]
    search_embedder = embedder if embedder is not None else LocalHashEmbedder()
    hits, vector_refs = _hybrid_hits(session, scope, bounded, search_embedder)
    evidence = (*_decline_aggregates(session, scope), *(_hit_evidence(hit) for hit in hits))
    session.close()
    relevant = relevant_evidence(evidence, bounded, vector_refs)
    relevant_refs = tuple(item.ref for item in relevant)
    if not relevant:
        refusal = cannot_answer()
        return GroundedAssistantAnswer(
            answer=AssistantAnswer(mode="llm", text=refusal.text, evidence=evidence, model=None),
            grounding=refusal,
            relevant_refs=(),
        )
    try:
        response = llm.complete(LlmRequest(question=bounded, evidence=relevant, require_citations=True))
    except LlmUnavailableError:
        return GroundedAssistantAnswer(
            answer=AssistantAnswer(mode="t2_fallback", text=None, evidence=evidence, model=None),
            grounding=None,
            relevant_refs=relevant_refs,
        )
    grounding = ground_answer(response.text, relevant)
    return GroundedAssistantAnswer(
        answer=AssistantAnswer(mode="llm", text=grounding.text, evidence=evidence, model=response.model),
        grounding=grounding,
        relevant_refs=relevant_refs,
    )


def _hit_evidence(hit: SearchHit) -> EvidenceItem:
    return EvidenceItem(ref=f"{hit.kind}:{hit.ref_id}", summary=f"{hit.title}: {hit.body}")


def _hybrid_hits(
    session: Session, scope: Scope, question: str, embedder: Embedder
) -> tuple[list[SearchHit], frozenset[str]]:
    """hybrid_search's legs and fusion, keeping which refs the vector leg returned (at or above
    DEFAULT_MIN_SIMILARITY): the question is embedded once."""
    if not question.strip():
        return [], frozenset()
    candidates = max(SEARCH_LIMIT * 3, MIN_LEG_CANDIDATES)
    keyword = search_documents(session, scope, question, limit=candidates)
    try:
        vector = vector_search(
            session, scope, question, embedder, limit=candidates, min_similarity=DEFAULT_MIN_SIMILARITY
        )
    except EmbedderUnavailableError:
        vector = []
    fused = reciprocal_rank_fusion([keyword, vector], k=RRF_K, limit=SEARCH_LIMIT)
    return fused, frozenset(f"{hit.kind}:{hit.ref_id}" for hit in vector)
