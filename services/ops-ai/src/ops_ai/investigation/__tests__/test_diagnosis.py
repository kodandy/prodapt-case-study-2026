"""Tests for investigation/diagnosis.py, written from docs/specs/failure-tools.md section 7 (diagnose)."""

from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.assistant.grounding import Claim
from ops_ai.investigation.diagnosis import SIGNIFICANT_SHARE, Diagnosis, RankedFix, diagnose
from ops_ai.investigation.playbook import Playbook, PlaybookEntry, load_playbook
from ops_ai.investigation.reasons import TAXONOMY, ReasonBreakdown, ReasonGroup
from ops_ai.investigation.tools import (
    BaselineComparison,
    BinShare,
    ChangeEvent,
    ChangeTimeline,
    CodeCount,
    FailureAggregate,
    IncidentHit,
)

START = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)
END = START + timedelta(hours=6)
BASELINE_START = START - timedelta(days=7)
RESOLVED_AT = datetime(2026, 9, 14, 16, 40, tzinfo=UTC)


def _window(*, failed: int = 20, top_bin_failed: int = 14, with_bins: bool = True) -> FailureAggregate:
    bins = (
        (
            BinShare(card_bin="400000", failed=top_bin_failed, total=top_bin_failed + 2),
            BinShare(card_bin="411111", failed=failed - top_bin_failed, total=20),
        )
        if with_bins
        else ()
    )
    return FailureAggregate(
        start=START,
        end=END,
        total=40 if failed else 0,
        failed=failed,
        rate=failed / 40 if failed else 0.0,
        by_bin=bins,
        by_code=(CodeCount(decline_code="issuer_unavailable", count=failed),) if failed else (),
    )


BASELINE = FailureAggregate(
    start=BASELINE_START,
    end=START,
    total=400,
    failed=8,
    rate=8 / 400,
    by_bin=(BinShare(card_bin="411111", failed=8, total=400),),
    by_code=(CodeCount(decline_code="card_declined", count=8),),
)


def _comparison(*, elevated: bool, window: FailureAggregate | None = None) -> BaselineComparison:
    chosen = window if window is not None else _window()
    return BaselineComparison(window=chosen, baseline=BASELINE, ratio=25.0, elevated=elevated)


def _group(code: str, *, count: int, share: float) -> ReasonGroup:
    entry = TAXONOMY[code]
    return ReasonGroup(
        taxonomy_code=code,
        category=entry.category,
        owner=entry.owner,
        retryable=entry.retryable,
        count=count,
        share=share,
        sample_ids=(),
        raw_codes=(code,),
        source="rules",
    )


def _breakdown(*groups: ReasonGroup) -> ReasonBreakdown:
    return ReasonBreakdown(total_failed=sum(group.count for group in groups), groups=groups)


ISSUER = _group("issuer_unavailable", count=14, share=0.7)
DECLINED = _group("card_declined", count=5, share=0.25)
FUNDS = _group("insufficient_funds", count=1, share=0.05)
NO_CHANGES = ChangeTimeline(buckets=(), changes=())


def _incident(
    incident_id: str,
    *,
    title: str = "Nightly incident",
    root_cause: str = "Issuer returned timeouts on BIN 400000",
    fix_applied: str = "Rerouted BIN 400000 to acq_b",
) -> IncidentHit:
    return IncidentHit(
        incident_id=incident_id,
        title=title,
        root_cause=root_cause,
        fix_applied=fix_applied,
        resolved_at=RESOLVED_AT,
        recurrence_within_7d=False,
    )


@pytest.fixture
def playbook() -> Playbook:
    return load_playbook()


def _refs(claims: tuple[Claim, ...]) -> list[tuple[str, ...]]:
    return [claim.evidence_refs for claim in claims]


def _flat_refs(claims: tuple[Claim, ...]) -> list[str]:
    return [ref for claim in claims for ref in claim.evidence_refs]


# --- constants and return shape --------------------------------------------------------------------------


def test_significant_share_is_ten_percent() -> None:
    # Arrange / Act
    share = SIGNIFICANT_SHARE

    # Assert
    assert share == 0.1


def test_diagnose_returns_a_diagnosis_of_claim_tuples_and_ranked_fixes(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=True)

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER), [], playbook)

    # Assert
    assert isinstance(result, Diagnosis)
    assert all(isinstance(claim, Claim) for claim in result.facts + result.hypotheses + result.how_to_confirm)
    assert all(isinstance(fix, RankedFix) for fix in result.fixes)


# --- not elevated ----------------------------------------------------------------------------------------


def test_diagnose_not_elevated_gives_no_hypotheses_confirmation_steps_or_fixes(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=False)
    breakdown = _breakdown(ISSUER, DECLINED)

    # Act
    result = diagnose(comparison, NO_CHANGES, breakdown, [_incident("inc_1")], playbook)

    # Assert
    assert result.hypotheses == ()
    assert result.how_to_confirm == ()
    assert result.fixes == ()


def test_diagnose_not_elevated_states_the_rate_is_within_the_baseline_citing_the_comparison(
    playbook: Playbook,
) -> None:
    # Arrange
    comparison = _comparison(elevated=False, window=_window(failed=0, with_bins=False))

    # Act
    result = diagnose(comparison, NO_CHANGES, ReasonBreakdown(total_failed=0, groups=()), [], playbook)

    # Assert
    within = [
        claim
        for claim in result.facts
        if claim.evidence_refs == ("comparison:baseline",) and "within" in claim.text.lower()
    ]
    assert len(within) >= 1


def test_diagnose_not_elevated_still_states_the_window_and_baseline_rates(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=False)

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(), [], playbook)

    # Assert
    refs = _flat_refs(result.facts)
    assert refs.count("aggregate:window") == 1
    assert refs.count("aggregate:baseline") == 1


def test_diagnose_elevated_does_not_claim_the_rate_is_within_the_baseline(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=True)

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER), [], playbook)

    # Assert
    within = [claim for claim in result.facts if "within" in claim.text.lower()]
    assert within == []


def test_diagnose_not_elevated_still_lists_the_significant_reason_facts(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=False)

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER, FUNDS), [], playbook)

    # Assert
    refs = _flat_refs(result.facts)
    assert "reason:issuer_unavailable" in refs
    assert "reason:insufficient_funds" not in refs


# --- facts -----------------------------------------------------------------------------------------------


def test_diagnose_elevated_facts_cite_window_baseline_comparison_bin_changes_and_significant_reasons(
    playbook: Playbook,
) -> None:
    # Arrange
    timeline = ChangeTimeline(
        buckets=(),
        changes=(
            ChangeEvent(
                at=START + timedelta(hours=4), kind="failure_onset", subject="card", detail="Failures began"
            ),
            ChangeEvent(
                at=START + timedelta(hours=4, minutes=2),
                kind="bin_onset",
                subject="400000",
                detail="BIN 400000 began failing",
            ),
        ),
    )
    breakdown = _breakdown(ISSUER, DECLINED, FUNDS)

    # Act
    result = diagnose(_comparison(elevated=True), timeline, breakdown, [], playbook)

    # Assert
    assert _refs(result.facts) == [
        ("aggregate:window",),
        ("aggregate:baseline",),
        ("comparison:baseline",),
        ("aggregate:bin:400000",),
        ("change:failure_onset:card",),
        ("change:bin_onset:400000",),
        ("reason:issuer_unavailable",),
        ("reason:card_declined",),
    ]


def test_diagnose_facts_have_non_empty_text(playbook: Playbook) -> None:
    # Arrange
    breakdown = _breakdown(ISSUER, DECLINED)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert [claim.text.strip() != "" for claim in result.facts] == [True] * len(result.facts)


def test_diagnose_omits_the_bin_fact_when_the_window_has_no_failures(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=False, window=_window(failed=0, with_bins=False))

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(), [], playbook)

    # Assert
    assert [ref for ref in _flat_refs(result.facts) if ref.startswith("aggregate:bin:")] == []


def test_diagnose_omits_the_bin_fact_when_the_window_has_failures_but_no_bin(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=True, window=_window(failed=20, with_bins=False))

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER), [], playbook)

    # Assert
    assert [ref for ref in _flat_refs(result.facts) if ref.startswith("aggregate:bin:")] == []


def test_diagnose_bin_fact_cites_the_top_bin_even_when_it_holds_under_half_the_failures(
    playbook: Playbook,
) -> None:
    # Arrange: 9 of 20 failures
    comparison = _comparison(elevated=True, window=_window(failed=20, top_bin_failed=9))

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER), [], playbook)

    # Assert
    assert [ref for ref in _flat_refs(result.facts) if ref.startswith("aggregate:bin:")] == [
        "aggregate:bin:400000"
    ]


def test_diagnose_gives_one_fact_per_change_event_in_timeline_order(playbook: Playbook) -> None:
    # Arrange
    timeline = ChangeTimeline(
        buckets=(),
        changes=(
            ChangeEvent(at=START, kind="incident_started", subject="inc_9", detail="Incident inc_9 started"),
            ChangeEvent(
                at=START + timedelta(minutes=5),
                kind="new_decline_code",
                subject="issuer_unavailable",
                detail="New decline code",
            ),
        ),
    )

    # Act
    result = diagnose(_comparison(elevated=True), timeline, _breakdown(), [], playbook)

    # Assert
    change_refs = [ref for ref in _flat_refs(result.facts) if ref.startswith("change:")]
    assert change_refs == ["change:incident_started:inc_9", "change:new_decline_code:issuer_unavailable"]


@pytest.mark.parametrize("code", sorted(TAXONOMY))
def test_diagnose_reason_fact_states_share_code_owner_and_retryability(playbook: Playbook, code: str) -> None:
    # Arrange
    entry = TAXONOMY[code]
    group = _group(code, count=20, share=1.0)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(group), [], playbook)

    # Assert
    retryable = "yes" if entry.retryable else "no"
    facts = [claim for claim in result.facts if claim.evidence_refs == (f"reason:{code}",)]
    assert len(facts) == 1
    assert f"are {code} (owner {entry.owner}, retryable {retryable})" in facts[0].text


def test_diagnose_includes_a_reason_with_exactly_the_significant_share_and_excludes_one_just_under(
    playbook: Playbook,
) -> None:
    # Arrange
    at_threshold = _group("expired_card", count=2, share=0.1)
    just_under = _group("incorrect_cvc", count=2, share=0.0999)

    # Act
    result = diagnose(
        _comparison(elevated=True), NO_CHANGES, _breakdown(at_threshold, just_under), [], playbook
    )

    # Assert
    assert [ref for ref in _flat_refs(result.facts) if ref.startswith("reason:")] == ["reason:expired_card"]
    assert [fix.taxonomy_code for fix in result.fixes] == ["expired_card"]


# --- hypotheses, confirmation steps, fixes ---------------------------------------------------------------


def test_diagnose_elevated_gives_one_hypothesis_per_significant_group_with_the_playbook_text(
    playbook: Playbook,
) -> None:
    # Arrange
    breakdown = _breakdown(ISSUER, DECLINED, FUNDS)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert [claim.text for claim in result.hypotheses] == [
        playbook.entries["issuer_unavailable"].hypothesis,
        playbook.entries["card_declined"].hypothesis,
    ]


def test_diagnose_hypothesis_cites_the_reason_and_the_top_bin_when_it_holds_most_failures(
    playbook: Playbook,
) -> None:
    # Arrange: bin 400000 holds 14 of 20 failures
    breakdown = _breakdown(ISSUER, DECLINED)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert _refs(result.hypotheses) == [
        ("reason:issuer_unavailable", "aggregate:bin:400000"),
        ("reason:card_declined", "aggregate:bin:400000"),
    ]


@pytest.mark.parametrize(
    ("top_bin_failed", "expect_bin_ref"),
    [(10, True), (11, True), (9, False), (5, False)],
    ids=["exactly_half", "just_over_half", "just_under_half", "quarter"],
)
def test_diagnose_adds_the_top_bin_ref_to_hypotheses_only_when_it_holds_at_least_half_the_failures(
    playbook: Playbook, top_bin_failed: int, expect_bin_ref: bool
) -> None:
    # Arrange: 20 window failures
    comparison = _comparison(elevated=True, window=_window(failed=20, top_bin_failed=top_bin_failed))

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER), [], playbook)

    # Assert
    expected = (
        [("reason:issuer_unavailable", "aggregate:bin:400000")]
        if expect_bin_ref
        else [("reason:issuer_unavailable",)]
    )
    assert _refs(result.hypotheses) == expected


def test_diagnose_hypothesis_has_no_bin_ref_when_the_window_has_no_bins(playbook: Playbook) -> None:
    # Arrange
    comparison = _comparison(elevated=True, window=_window(failed=20, with_bins=False))

    # Act
    result = diagnose(comparison, NO_CHANGES, _breakdown(ISSUER), [], playbook)

    # Assert
    assert _refs(result.hypotheses) == [("reason:issuer_unavailable",)]


def test_diagnose_gives_one_confirmation_claim_per_playbook_step_citing_the_reason(
    playbook: Playbook,
) -> None:
    # Arrange
    breakdown = _breakdown(ISSUER, DECLINED, FUNDS)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    issuer_steps = playbook.entries["issuer_unavailable"].how_to_confirm
    declined_steps = playbook.entries["card_declined"].how_to_confirm
    assert [claim.text for claim in result.how_to_confirm] == [*issuer_steps, *declined_steps]
    assert _refs(result.how_to_confirm) == [
        *([("reason:issuer_unavailable",)] * len(issuer_steps)),
        *([("reason:card_declined",)] * len(declined_steps)),
    ]


def test_diagnose_ranks_fixes_from_one_in_group_order_with_the_playbook_fix_and_version(
    playbook: Playbook,
) -> None:
    # Arrange
    breakdown = _breakdown(ISSUER, DECLINED, FUNDS)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert result.fixes == (
        RankedFix(
            rank=1,
            taxonomy_code="issuer_unavailable",
            fix=playbook.entries["issuer_unavailable"].fix,
            playbook_version=playbook.version,
            evidence_refs=("reason:issuer_unavailable",),
            incident_refs=(),
        ),
        RankedFix(
            rank=2,
            taxonomy_code="card_declined",
            fix=playbook.entries["card_declined"].fix,
            playbook_version=playbook.version,
            evidence_refs=("reason:card_declined",),
            incident_refs=(),
        ),
    )


def test_diagnose_follows_the_group_order_it_is_given_rather_than_re_sorting(playbook: Playbook) -> None:
    # Arrange
    breakdown = ReasonBreakdown(total_failed=19, groups=(DECLINED, ISSUER))

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert [(fix.rank, fix.taxonomy_code) for fix in result.fixes] == [
        (1, "card_declined"),
        (2, "issuer_unavailable"),
    ]
    assert [ref for ref in _flat_refs(result.hypotheses) if ref.startswith("reason:")] == [
        "reason:card_declined",
        "reason:issuer_unavailable",
    ]


def test_diagnose_fix_evidence_never_includes_the_top_bin_ref(playbook: Playbook) -> None:
    # Arrange: the top bin holds 70 % of the failures
    breakdown = _breakdown(ISSUER)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert [fix.evidence_refs for fix in result.fixes] == [("reason:issuer_unavailable",)]


def test_diagnose_reports_the_playbook_version_of_the_playbook_it_was_given() -> None:
    # Arrange
    entries = {
        code: PlaybookEntry(
            taxonomy_code=code,
            fix=f"Custom fix for {code}",
            hypothesis=f"Custom hypothesis for {code}",
            how_to_confirm=(f"Custom check 1 for {code}", f"Custom check 2 for {code}"),
        )
        for code in TAXONOMY
    }
    custom = Playbook(version="custom.v9", entries=entries)

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), [], custom)

    # Assert
    assert [claim.text for claim in result.hypotheses] == ["Custom hypothesis for issuer_unavailable"]
    assert [claim.text for claim in result.how_to_confirm] == [
        "Custom check 1 for issuer_unavailable",
        "Custom check 2 for issuer_unavailable",
    ]
    assert result.fixes == (
        RankedFix(
            rank=1,
            taxonomy_code="issuer_unavailable",
            fix="Custom fix for issuer_unavailable",
            playbook_version="custom.v9",
            evidence_refs=("reason:issuer_unavailable",),
            incident_refs=(),
        ),
    )


@pytest.mark.parametrize("code", sorted(TAXONOMY))
def test_diagnose_covers_every_taxonomy_code_from_the_shipped_playbook(playbook: Playbook, code: str) -> None:
    # Arrange
    entry = playbook.entries[code]
    breakdown = _breakdown(_group(code, count=20, share=1.0))

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert [claim.text for claim in result.hypotheses] == [entry.hypothesis]
    assert [claim.text for claim in result.how_to_confirm] == list(entry.how_to_confirm)
    assert [(fix.rank, fix.taxonomy_code, fix.fix) for fix in result.fixes] == [(1, code, entry.fix)]


def test_diagnose_elevated_without_any_reason_group_gives_facts_only(playbook: Playbook) -> None:
    # Arrange
    breakdown = ReasonBreakdown(total_failed=0, groups=())

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert result.hypotheses == ()
    assert result.how_to_confirm == ()
    assert result.fixes == ()
    assert _refs(result.facts)[:3] == [
        ("aggregate:window",),
        ("aggregate:baseline",),
        ("comparison:baseline",),
    ]


def test_diagnose_elevated_with_only_insignificant_groups_gives_no_hypotheses_or_fixes(
    playbook: Playbook,
) -> None:
    # Arrange
    breakdown = _breakdown(
        _group("expired_card", count=1, share=0.05), _group("incorrect_cvc", count=1, share=0.05)
    )

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, breakdown, [], playbook)

    # Assert
    assert result.hypotheses == ()
    assert result.how_to_confirm == ()
    assert result.fixes == ()


# --- incidents -------------------------------------------------------------------------------------------


def test_diagnose_links_an_incident_whose_root_cause_classifies_to_the_same_code_and_appends_its_fix(
    playbook: Playbook,
) -> None:
    # Arrange
    incident = _incident(
        "inc_1", root_cause="Issuer returned timeouts on BIN 400000", fix_applied="Rerouted BIN 400000"
    )

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), [incident], playbook)

    # Assert
    assert result.fixes == (
        RankedFix(
            rank=1,
            taxonomy_code="issuer_unavailable",
            fix=f"{playbook.entries['issuer_unavailable'].fix} Previously applied: Rerouted BIN 400000",
            playbook_version=playbook.version,
            evidence_refs=("reason:issuer_unavailable",),
            incident_refs=("incident:inc_1",),
        ),
    )


def test_diagnose_links_an_incident_whose_title_classifies_when_its_root_cause_does_not(
    playbook: Playbook,
) -> None:
    # Arrange
    incident = _incident(
        "inc_title",
        title="Issuer timeout on BIN 400000",
        root_cause="Under investigation",
        fix_applied="Failed over to the backup acquirer",
    )

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), [incident], playbook)

    # Assert
    assert result.fixes[0].incident_refs == ("incident:inc_title",)
    assert result.fixes[0].fix.endswith(" Previously applied: Failed over to the backup acquirer")


def test_diagnose_ignores_an_incident_that_classifies_to_another_code(playbook: Playbook) -> None:
    # Arrange
    incident = _incident(
        "inc_other", root_cause="Payments declined at the issuer", fix_applied="Asked customers to retry"
    )

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), [incident], playbook)

    # Assert
    assert result.fixes[0].incident_refs == ()
    assert result.fixes[0].fix == playbook.entries["issuer_unavailable"].fix


def test_diagnose_ignores_an_incident_that_classifies_to_no_code(playbook: Playbook) -> None:
    # Arrange
    incident = _incident(
        "inc_unknown", title="Dashboard slowness", root_cause="Chargeback backlog", fix_applied="Hired"
    )

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), [incident], playbook)

    # Assert
    assert result.fixes[0].incident_refs == ()
    assert result.fixes[0].fix == playbook.entries["issuer_unavailable"].fix


def test_diagnose_attaches_each_incident_only_to_the_fix_of_its_own_code(playbook: Playbook) -> None:
    # Arrange
    timeout_incident = _incident("inc_timeout", fix_applied="Rerouted BIN 400000")
    declined_incident = _incident(
        "inc_declined", root_cause="Payments declined at the issuer", fix_applied="Asked customers to retry"
    )

    # Act
    result = diagnose(
        _comparison(elevated=True),
        NO_CHANGES,
        _breakdown(ISSUER, DECLINED),
        [timeout_incident, declined_incident],
        playbook,
    )

    # Assert
    assert [(fix.taxonomy_code, fix.incident_refs) for fix in result.fixes] == [
        ("issuer_unavailable", ("incident:inc_timeout",)),
        ("card_declined", ("incident:inc_declined",)),
    ]
    assert result.fixes[0].fix == (
        f"{playbook.entries['issuer_unavailable'].fix} Previously applied: Rerouted BIN 400000"
    )
    assert result.fixes[1].fix == (
        f"{playbook.entries['card_declined'].fix} Previously applied: Asked customers to retry"
    )


def test_diagnose_lists_every_matching_incident_ref(playbook: Playbook) -> None:
    # Arrange
    incidents = [_incident("inc_1"), _incident("inc_2", fix_applied="Switched acquirer")]

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), incidents, playbook)

    # Assert
    assert sorted(result.fixes[0].incident_refs) == ["incident:inc_1", "incident:inc_2"]
    prefix = f"{playbook.entries['issuer_unavailable'].fix} Previously applied: "
    assert result.fixes[0].fix.startswith(prefix)


def test_diagnose_does_not_use_incidents_for_a_group_below_the_significant_share(playbook: Playbook) -> None:
    # Arrange
    incident = _incident("inc_1")

    # Act
    result = diagnose(
        _comparison(elevated=True),
        NO_CHANGES,
        _breakdown(DECLINED, _group("issuer_unavailable", count=1, share=0.05)),
        [incident],
        playbook,
    )

    # Assert
    assert [(fix.taxonomy_code, fix.incident_refs) for fix in result.fixes] == [("card_declined", ())]


def test_diagnose_leaves_the_hypothesis_and_confirmation_text_untouched_by_incidents(
    playbook: Playbook,
) -> None:
    # Arrange
    incident = _incident("inc_1")

    # Act
    result = diagnose(_comparison(elevated=True), NO_CHANGES, _breakdown(ISSUER), [incident], playbook)

    # Assert
    assert [claim.text for claim in result.hypotheses] == [playbook.entries["issuer_unavailable"].hypothesis]
    assert [claim.text for claim in result.how_to_confirm] == list(
        playbook.entries["issuer_unavailable"].how_to_confirm
    )
