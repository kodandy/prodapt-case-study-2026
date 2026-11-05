"""Tests for ops_ai/assistant/grounding.py, written from docs/specs/failure-tools.md section 3 (B5.2)."""

import pytest
from ops_ai.assistant.evidence import EvidenceItem
from ops_ai.assistant.grounding import ABSTAIN_TEXT, Claim, cited_refs, ground_claims, grounded_narration

EVIDENCE = (
    EvidenceItem(ref="aggregate:window", summary="Window summary"),
    EvidenceItem(ref="aggregate:baseline", summary="Baseline summary"),
    EvidenceItem(ref="reason:issuer_unavailable", summary="Reason summary"),
)


# --- ABSTAIN_TEXT ----------------------------------------------------------------------------------------


def test_abstain_text_is_the_specified_sentence() -> None:
    # Arrange / Act
    text = ABSTAIN_TEXT

    # Assert
    assert text == "Not enough data to answer this from the records available."


# --- ground_claims ---------------------------------------------------------------------------------------


def test_ground_claims_keeps_a_claim_whose_every_ref_is_an_evidence_ref() -> None:
    # Arrange
    claim = Claim(text="Rates differ", evidence_refs=("aggregate:window", "aggregate:baseline"))

    # Act
    result = ground_claims([claim], EVIDENCE)

    # Assert
    assert result == (claim,)


def test_ground_claims_drops_a_claim_without_refs() -> None:
    # Arrange
    claim = Claim(text="Trust me", evidence_refs=())

    # Act
    result = ground_claims([claim], EVIDENCE)

    # Assert
    assert result == ()


def test_ground_claims_drops_a_claim_whose_only_ref_is_unknown() -> None:
    # Arrange
    claim = Claim(text="Made up", evidence_refs=("payment:does-not-exist",))

    # Act
    result = ground_claims([claim], EVIDENCE)

    # Assert
    assert result == ()


def test_ground_claims_drops_a_claim_when_any_one_of_its_refs_is_unknown() -> None:
    # Arrange
    claim = Claim(text="Half grounded", evidence_refs=("aggregate:window", "payment:does-not-exist"))

    # Act
    result = ground_claims([claim], EVIDENCE)

    # Assert
    assert result == ()


def test_ground_claims_drops_every_claim_when_the_evidence_is_empty() -> None:
    # Arrange
    claim = Claim(text="Rates differ", evidence_refs=("aggregate:window",))

    # Act
    result = ground_claims([claim], ())

    # Assert
    assert result == ()


def test_ground_claims_returns_an_empty_tuple_for_no_claims() -> None:
    # Arrange / Act
    result = ground_claims([], EVIDENCE)

    # Assert
    assert result == ()


def test_ground_claims_collapses_duplicate_refs_inside_a_claim_keeping_the_first_order() -> None:
    # Arrange
    claim = Claim(
        text="Repeated refs",
        evidence_refs=("aggregate:baseline", "aggregate:window", "aggregate:baseline", "aggregate:window"),
    )

    # Act
    result = ground_claims([claim], EVIDENCE)

    # Assert
    assert result == (Claim(text="Repeated refs", evidence_refs=("aggregate:baseline", "aggregate:window")),)


def test_ground_claims_keeps_the_claim_order_and_drops_only_the_ungrounded_ones() -> None:
    # Arrange
    first = Claim(text="first", evidence_refs=("reason:issuer_unavailable",))
    ungrounded = Claim(text="second", evidence_refs=("incident:missing",))
    third = Claim(text="third", evidence_refs=("aggregate:window",))
    no_refs = Claim(text="fourth", evidence_refs=())
    fifth = Claim(text="fifth", evidence_refs=("aggregate:baseline",))

    # Act
    result = ground_claims([first, ungrounded, third, no_refs, fifth], EVIDENCE)

    # Assert
    assert result == (first, third, fifth)


def test_ground_claims_keeps_two_claims_that_cite_the_same_ref() -> None:
    # Arrange
    one = Claim(text="one", evidence_refs=("aggregate:window",))
    two = Claim(text="two", evidence_refs=("aggregate:window",))

    # Act
    result = ground_claims([one, two], EVIDENCE)

    # Assert
    assert result == (one, two)


def test_ground_claims_accepts_any_iterable_of_claims() -> None:
    # Arrange
    claims = (Claim(text=f"claim {n}", evidence_refs=("aggregate:window",)) for n in range(3))

    # Act
    result = ground_claims(claims, EVIDENCE)

    # Assert
    assert [claim.text for claim in result] == ["claim 0", "claim 1", "claim 2"]


def test_ground_claims_matches_refs_exactly_and_case_sensitively() -> None:
    # Arrange
    upper = Claim(text="upper", evidence_refs=("Aggregate:Window",))
    prefix = Claim(text="prefix", evidence_refs=("aggregate:win",))
    padded = Claim(text="padded", evidence_refs=(" aggregate:window",))

    # Act
    result = ground_claims([upper, prefix, padded], EVIDENCE)

    # Assert
    assert result == ()


# --- cited_refs ------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Window failed [aggregate:window] against [aggregate:baseline].",
            ("aggregate:window", "aggregate:baseline"),
        ),
        ("[a:1][b:2][c:3]", ("a:1", "b:2", "c:3")),
        ("[b:2] then [a:1] then [b:2] again", ("b:2", "a:1")),
        ("[aggregate:window] [aggregate:window] [aggregate:window]", ("aggregate:window",)),
        ("no citations here", ()),
        ("", ()),
        ("empty brackets [] are skipped", ()),
        ("unclosed [aggregate:window", ()),
        ("unopened aggregate:window]", ()),
        ("outer [a [inner:ref] b] text", ("inner:ref",)),
        ("two [x:1] [] [y:2]", ("x:1", "y:2")),
    ],
    ids=[
        "two_refs_in_prose",
        "adjacent_refs",
        "duplicates_keep_first_position",
        "same_ref_three_times",
        "no_brackets",
        "empty_text",
        "empty_brackets",
        "unclosed_bracket",
        "unopened_bracket",
        "nested_brackets_keep_the_innermost",
        "empty_brackets_between_refs",
    ],
)
def test_cited_refs_returns_each_bracketed_token_in_order_without_duplicates(
    text: str, expected: tuple[str, ...]
) -> None:
    # Arrange / Act
    result = cited_refs(text)

    # Assert
    assert result == expected


def test_cited_refs_returns_a_tuple() -> None:
    # Arrange / Act
    result = cited_refs("see [aggregate:window]")

    # Assert
    assert isinstance(result, tuple)
    assert result == ("aggregate:window",)


# --- grounded_narration ----------------------------------------------------------------------------------


def test_grounded_narration_returns_the_text_when_it_cites_a_known_ref() -> None:
    # Arrange
    text = "Failures rose sharply [aggregate:window]."

    # Act
    result = grounded_narration(text, EVIDENCE)

    # Assert
    assert result == text


def test_grounded_narration_returns_the_text_unchanged_when_every_cited_ref_is_known() -> None:
    # Arrange
    text = "  One [aggregate:window]\nTwo [reason:issuer_unavailable] and [aggregate:window] again \n"

    # Act
    result = grounded_narration(text, EVIDENCE)

    # Assert
    assert result == text


def test_grounded_narration_is_none_when_the_text_cites_nothing() -> None:
    # Arrange
    text = "Failures rose sharply."

    # Act
    result = grounded_narration(text, EVIDENCE)

    # Assert
    assert result is None


def test_grounded_narration_is_none_when_the_only_cited_ref_is_unknown() -> None:
    # Arrange
    text = "Failures rose sharply [payment:does-not-exist]."

    # Act
    result = grounded_narration(text, EVIDENCE)

    # Assert
    assert result is None


def test_grounded_narration_is_none_when_one_of_several_cited_refs_is_unknown() -> None:
    # Arrange
    text = "Failures rose [aggregate:window] because of [incident:missing] and [aggregate:baseline]."

    # Act
    result = grounded_narration(text, EVIDENCE)

    # Assert
    assert result is None


def test_grounded_narration_is_none_for_empty_text() -> None:
    # Arrange / Act
    result = grounded_narration("", EVIDENCE)

    # Assert
    assert result is None


def test_grounded_narration_is_none_when_there_is_no_evidence() -> None:
    # Arrange / Act
    result = grounded_narration("Failures rose [aggregate:window].", ())

    # Assert
    assert result is None
