from collections.abc import Sequence
from dataclasses import dataclass

from ops_ai.assistant.grounding import Claim
from ops_ai.investigation.playbook import Playbook
from ops_ai.investigation.reasons import ReasonBreakdown, classify_reason
from ops_ai.investigation.tools import BaselineComparison, ChangeTimeline, IncidentHit

SIGNIFICANT_SHARE = 0.1


@dataclass(frozen=True)
class RankedFix:
    rank: int
    taxonomy_code: str
    fix: str
    playbook_version: str
    evidence_refs: tuple[str, ...]
    incident_refs: tuple[str, ...]


@dataclass(frozen=True)
class Diagnosis:
    facts: tuple[Claim, ...]
    hypotheses: tuple[Claim, ...]
    how_to_confirm: tuple[Claim, ...]
    fixes: tuple[RankedFix, ...]


def diagnose(
    comparison: BaselineComparison,
    timeline: ChangeTimeline,
    breakdown: ReasonBreakdown,
    incidents: Sequence[IncidentHit],
    playbook: Playbook,
) -> Diagnosis:
    window, baseline = comparison.window, comparison.baseline
    facts = [
        Claim(
            f"Window: {window.failed} of {window.total} card payments failed ({window.rate:.1%}).",
            ("aggregate:window",),
        ),
        Claim(
            f"Baseline: {baseline.failed} of {baseline.total} card payments failed ({baseline.rate:.1%}).",
            ("aggregate:baseline",),
        ),
        Claim(
            "The failure rate is elevated."
            if comparison.elevated
            else "The failure rate is within the baseline.",
            ("comparison:baseline",),
        ),
    ]
    top_bin = window.by_bin[0] if window.by_bin and window.failed else None
    if top_bin is not None:
        facts.append(
            Claim(
                f"BIN {top_bin.card_bin} accounts for {top_bin.failed / window.failed:.1%} of failures.",
                (f"aggregate:bin:{top_bin.card_bin}",),
            )
        )
    facts.extend(
        Claim(change.detail, (f"change:{change.kind}:{change.subject}",)) for change in timeline.changes
    )
    significant = [group for group in breakdown.groups if group.share >= SIGNIFICANT_SHARE]
    facts.extend(
        Claim(
            f"{group.share:.1%} of failures are {group.taxonomy_code} "
            f"(owner {group.owner}, retryable {'yes' if group.retryable else 'no'})",
            (f"reason:{group.taxonomy_code}",),
        )
        for group in significant
    )
    hypotheses: list[Claim] = []
    confirmations: list[Claim] = []
    fixes: list[RankedFix] = []
    if comparison.elevated:
        for group in significant:
            entry = playbook.entries[group.taxonomy_code]
            reason_refs = (f"reason:{group.taxonomy_code}",)
            bin_refs = (
                (f"aggregate:bin:{top_bin.card_bin}",)
                if top_bin is not None and 2 * top_bin.failed >= window.failed
                else ()
            )
            hypotheses.append(Claim(entry.hypothesis, reason_refs + bin_refs))
            confirmations.extend(Claim(step, reason_refs) for step in entry.how_to_confirm)
            matching = [
                hit
                for hit in incidents
                if any(
                    classified is not None and classified.code == group.taxonomy_code
                    for classified in (classify_reason(hit.root_cause), classify_reason(hit.title))
                )
            ]
            fix = entry.fix + "".join(f" Previously applied: {hit.fix_applied}" for hit in matching)
            fixes.append(
                RankedFix(
                    len(fixes) + 1,
                    group.taxonomy_code,
                    fix,
                    playbook.version,
                    reason_refs,
                    tuple(f"incident:{hit.incident_id}" for hit in matching),
                )
            )
    return Diagnosis(tuple(facts), tuple(hypotheses), tuple(confirmations), tuple(fixes))
