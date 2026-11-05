"""Root-cause hypothesis ranking by evidence support (docs/specs/evaluation-judge.md section 6.2)."""

import json
import re
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, StrictStr

from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.llm_port import LlmPort, LlmRequest, LlmUnavailableError
from ops_ai.judge.fake import RUBRIC_JSON_PREFIX
from ops_ai.judge.llm_parse import parse_reply
from ops_ai.judge.schemas import Hypothesis, HypothesisRanking, LlmStatus, RankedHypothesis

__all__ = ["PLAYBOOK", "HypothesisRanking", "RankedHypothesis", "rank_hypotheses", "ref_weight"]

FABRICATED_PENALTY = 2.0
_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("aggregate:bin:", 3.0),
    ("aggregate:window", 2.0),
    ("incident:", 2.0),
    ("aggregate:baseline", 1.0),
    ("payment:", 0.5),
)
_FIX = re.compile(r"Fix:\s*(?P<fix>.+)$", re.DOTALL)
_INCIDENT = "incident:"

PLAYBOOK: Mapping[str, tuple[str, ...]] = {
    "issuer_outage_on_bin": (
        "Route payments for the affected BIN to the secondary acquirer or retry them with backoff.",
        "Tell affected customers to retry later or use another card.",
        "Confirm recovery when the BIN's failure rate returns to the baseline.",
    ),
    "acquirer_outage": (
        "Fail over card traffic to the secondary acquirer.",
        "Open a ticket with the acquirer and track its status page.",
        "Confirm recovery when the failure rate across all BINs returns to the baseline.",
    ),
    "elevated_failures": (
        "Break the failures down by decline code, BIN and acquirer to find the dominant driver.",
        "Confirm the driver by comparing the failure window with the 7-day baseline.",
    ),
    "merchant_integration_error": (
        "Check the merchant's recent checkout or API changes and roll back the faulty release.",
        "Confirm the fix with a test payment through the merchant's checkout.",
    ),
    "insufficient_funds": (
        "Ask the customer to use another payment method or retry after funding the account.",
        "Confirm by checking that later attempts from the same customer succeed.",
    ),
    "do_not_honor": (
        "Ask the customer to contact their issuer or use another card; do not retry automatically.",
        "Confirm by checking that the customer's next attempt with another card succeeds.",
    ),
    "expired_card": (
        "Ask the customer to update the card details or enable the card updater service.",
        "Confirm by checking that the retry with the updated card succeeds.",
    ),
    "incorrect_cvc": (
        "Ask the customer to re-enter the security code; block repeated CVC failures on the same card.",
        "Confirm by checking that the next attempt passes CVC verification.",
    ),
    "fraud_flagged": (
        "Keep the flagged payments on hold and review them in the fraud console.",
        "Confirm by checking the review outcome for each flagged payment.",
    ),
    "card_testing": (
        "Block the attacking cards or source, and add velocity limits and CAPTCHA on the checkout.",
        "Refund or void any successful micro payments from the burst.",
        "Confirm by watching that no new micro-amount burst is flagged in the next hour.",
    ),
    "cross_border_limit_for_kyc_tier": (
        "Ask the sender to upgrade to KYC tier 2 or split the transfer below the tier limit.",
        "Confirm by retrying the transfer after the tier upgrade.",
    ),
}

RANKING_INSTRUCTIONS = (
    "You are a root-cause judge. The hypotheses below are already scored by evidence support. Order them by "
    "how well the EVIDENCE supports each one and give a short rationale per id. You may not add, drop or "
    'rewrite hypotheses. Reply with JSON only: {"order": [<every hypothesis_id exactly once>], '
    '"rationales": {"<hypothesis_id>": "<=300 characters"}}.'
)


class _LlmRanking(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    order: tuple[StrictStr, ...]
    rationales: dict[StrictStr, StrictStr]


def ref_weight(ref: str) -> float:
    for prefix, weight in _WEIGHTS:
        if ref.startswith(prefix):
            return weight
    return 1.0


def _fix_steps(valid_refs: Sequence[str], summaries: Mapping[str, str]) -> list[str]:
    steps: list[str] = []
    for ref in valid_refs:
        if not ref.startswith(_INCIDENT):
            continue
        match = _FIX.search(summaries[ref])
        if match is not None:
            steps.append(
                f"Apply the fix from incident {ref.removeprefix(_INCIDENT)}: {match.group('fix').strip()}"
            )
    return steps


def _resolution_steps(
    cause_code: str, valid_refs: Sequence[str], summaries: Mapping[str, str]
) -> tuple[str, ...]:
    playbook = PLAYBOOK.get(cause_code)
    if playbook is None:
        return (f"Collect more evidence before acting: no playbook entry for {cause_code}.",)
    return (*playbook, *_fix_steps(valid_refs, summaries))


def _score(hypothesis: Hypothesis, summaries: Mapping[str, str]) -> RankedHypothesis:
    cited = list(dict.fromkeys(hypothesis.evidence_refs))
    valid = tuple(ref for ref in cited if ref in summaries)
    fabricated = tuple(ref for ref in cited if ref not in summaries)
    return RankedHypothesis(
        rank=1,
        hypothesis_id=hypothesis.hypothesis_id,
        cause_code=hypothesis.cause_code,
        statement=hypothesis.statement,
        support_score=sum(ref_weight(ref) for ref in valid) - FABRICATED_PENALTY * len(fabricated),
        supported=bool(valid) and not fabricated,
        valid_refs=valid,
        fabricated_refs=fabricated,
        resolution_steps=_resolution_steps(hypothesis.cause_code, valid, summaries),
        llm_rationale=None,
    )


def _prompt(scored: Sequence[RankedHypothesis]) -> str:
    listing = json.dumps(
        [
            {
                "hypothesis_id": item.hypothesis_id,
                "cause_code": item.cause_code,
                "statement": item.statement,
                "support_score": item.support_score,
                "valid_refs": list(item.valid_refs),
                "fabricated_refs": list(item.fabricated_refs),
            }
            for item in scored
        ]
    )
    deterministic = sorted(range(len(scored)), key=lambda i: (-scored[i].support_score, i))
    agreeing = json.dumps({"order": [scored[i].hypothesis_id for i in deterministic], "rationales": {}})
    return "\n".join((RANKING_INSTRUCTIONS, f"HYPOTHESES: {listing}", f"{RUBRIC_JSON_PREFIX} {agreeing}"))


def _valid_llm_ranking(reply: _LlmRanking | None, ids: Sequence[str]) -> _LlmRanking | None:
    if reply is None:
        return None
    if len(reply.order) != len(ids) or set(reply.order) != set(ids):
        return None
    if not set(reply.rationales) <= set(ids):
        return None
    if any(len(text) > 300 for text in reply.rationales.values()):
        return None
    return reply


def rank_hypotheses(
    hypotheses: Sequence[Hypothesis], evidence: Sequence[EvidenceItem], llm: LlmPort | None = None
) -> HypothesisRanking:
    """Deterministic support score decides; the LLM may only break ties and attach rationales."""
    ids = [hypothesis.hypothesis_id for hypothesis in hypotheses]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate hypothesis_id")
    if not hypotheses:
        return HypothesisRanking(ranked=(), llm_status="skipped", model=None)
    summaries: dict[str, str] = {}
    for item in evidence:
        summaries.setdefault(item.ref, item.summary)
    scored = [_score(hypothesis, summaries) for hypothesis in hypotheses]

    status: LlmStatus = "skipped"
    model: str | None = None
    llm_position: dict[str, int] = {}
    rationales: dict[str, str] = {}
    if llm is not None:
        try:
            response = llm.complete(LlmRequest(question=_prompt(scored), evidence=tuple(evidence)))
        except LlmUnavailableError:
            status = "unavailable"
        else:
            model = response.model
            reply = _valid_llm_ranking(parse_reply(response.text, _LlmRanking), ids)
            if reply is None:
                status = "rejected"
            else:
                status = "used"
                llm_position = {hypothesis_id: index for index, hypothesis_id in enumerate(reply.order)}
                rationales = dict(reply.rationales)

    order = sorted(
        range(len(scored)),
        key=lambda i: (-scored[i].support_score, llm_position.get(scored[i].hypothesis_id, 0), i),
    )
    ranked = tuple(
        scored[i].model_copy(update={"rank": rank, "llm_rationale": rationales.get(scored[i].hypothesis_id)})
        for rank, i in enumerate(order, start=1)
    )
    return HypothesisRanking(ranked=ranked, llm_status=status, model=model)
