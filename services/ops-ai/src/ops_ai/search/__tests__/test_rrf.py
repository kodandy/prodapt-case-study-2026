import uuid
from datetime import UTC, datetime, timedelta

import pytest
from ops_ai.search.service import DEFAULT_MIN_SIMILARITY, RRF_K, SearchHit, reciprocal_rank_fusion

BASE_TIME = datetime(2026, 11, 3, 11, 0, tzinfo=UTC)


def _id(number: int) -> uuid.UUID:
    return uuid.UUID(int=number)


def _hit(
    number: int,
    *,
    kind: str = "payment",
    title: str | None = None,
    occurred_at: datetime = BASE_TIME,
    rank: float = 0.0,
) -> SearchHit:
    return SearchHit(
        kind=kind,
        ref_id=_id(number),
        title=title or f"title {number}",
        body=f"body {number}",
        occurred_at=occurred_at,
        rank=rank,
    )


def _keys(hits: list[SearchHit]) -> list[tuple[str, uuid.UUID]]:
    return [(hit.kind, hit.ref_id) for hit in hits]


def test_constants_match_the_contract() -> None:
    # Arrange: constants imported from the module under test

    # Act
    constants = (RRF_K, DEFAULT_MIN_SIMILARITY)

    # Assert
    assert constants == (60, 0.4)


def test_a_single_ranking_keeps_its_order_and_scores_by_position() -> None:
    # Arrange
    ranking = [_hit(1), _hit(2), _hit(3)]

    # Act
    fused = reciprocal_rank_fusion([ranking])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(1), _id(2), _id(3)]
    assert [hit.rank for hit in fused] == pytest.approx([1 / 61, 1 / 62, 1 / 63], abs=1e-12)


def test_a_document_in_two_rankings_adds_the_reciprocal_of_each_position() -> None:
    # Arrange
    first = [_hit(1), _hit(2)]
    second = [_hit(3), _hit(1)]

    # Act
    fused = reciprocal_rank_fusion([first, second])

    # Assert
    scores = {hit.ref_id: hit.rank for hit in fused}
    assert scores[_id(1)] == pytest.approx(1 / 61 + 1 / 62, abs=1e-12)
    assert scores[_id(2)] == pytest.approx(1 / 62, abs=1e-12)
    assert scores[_id(3)] == pytest.approx(1 / 61, abs=1e-12)


def test_fused_order_puts_the_document_found_by_both_rankings_first() -> None:
    # Arrange
    first = [_hit(1), _hit(2)]
    second = [_hit(3), _hit(1)]

    # Act
    fused = reciprocal_rank_fusion([first, second])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(1), _id(3), _id(2)]


def test_two_lower_positions_beat_one_top_position() -> None:
    # Arrange
    first = [_hit(1), _hit(2)]
    second = [_hit(3), _hit(2)]

    # Act
    fused = reciprocal_rank_fusion([first, second])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(2), _id(1), _id(3)]
    assert fused[0].rank == pytest.approx(2 / 62, abs=1e-12)


def test_the_input_rank_values_do_not_change_the_fused_score() -> None:
    # Arrange
    ranking = [_hit(1, rank=0.001), _hit(2, rank=987.0)]

    # Act
    fused = reciprocal_rank_fusion([ranking])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(1), _id(2)]
    assert [hit.rank for hit in fused] == pytest.approx([1 / 61, 1 / 62], abs=1e-12)


def test_a_custom_k_changes_the_score_denominator() -> None:
    # Arrange
    ranking = [_hit(1), _hit(2)]

    # Act
    fused = reciprocal_rank_fusion([ranking], k=10)

    # Assert
    assert [hit.rank for hit in fused] == pytest.approx([1 / 11, 1 / 12], abs=1e-12)


def test_limit_cuts_the_fused_list_after_ordering() -> None:
    # Arrange
    first = [_hit(1), _hit(2), _hit(3)]
    second = [_hit(3), _hit(2), _hit(1)]

    # Act
    fused = reciprocal_rank_fusion([first, second], limit=2)

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(1), _id(3)]


def test_the_default_limit_is_ten() -> None:
    # Arrange
    ranking = [_hit(number) for number in range(1, 16)]

    # Act
    fused = reciprocal_rank_fusion([ranking])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(number) for number in range(1, 11)]


def test_equal_scores_are_ordered_by_most_recent_occurred_at_first() -> None:
    # Arrange
    older = _hit(1, occurred_at=BASE_TIME - timedelta(hours=1))
    newer = _hit(2, occurred_at=BASE_TIME)

    # Act
    fused = reciprocal_rank_fusion([[older], [newer]])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(2), _id(1)]
    assert fused[0].rank == fused[1].rank


def test_equal_scores_and_times_are_ordered_by_kind_then_ref_id_ascending() -> None:
    # Arrange
    payment_low = _hit(1, kind="payment")
    payment_high = _hit(9, kind="payment")
    incident = _hit(5, kind="incident")

    # Act
    fused = reciprocal_rank_fusion([[payment_high], [incident], [payment_low]])

    # Assert
    assert _keys(fused) == [("incident", _id(5)), ("payment", _id(1)), ("payment", _id(9))]


def test_equal_scores_kinds_and_times_are_ordered_by_ref_id_ascending() -> None:
    # Arrange
    low = _hit(0xA, kind="payment")
    high = _hit(0xB, kind="payment")

    # Act
    fused = reciprocal_rank_fusion([[high], [low]])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(0xA), _id(0xB)]


def test_the_same_ref_id_under_a_different_kind_is_a_different_document() -> None:
    # Arrange
    payment = _hit(1, kind="payment")
    incident = _hit(1, kind="incident")

    # Act
    fused = reciprocal_rank_fusion([[payment], [incident]])

    # Assert
    assert _keys(fused) == [("incident", _id(1)), ("payment", _id(1))]
    assert [hit.rank for hit in fused] == pytest.approx([1 / 61, 1 / 61], abs=1e-12)


def test_a_document_in_both_rankings_appears_once_with_the_fields_of_the_first_ranking() -> None:
    # Arrange
    from_first = _hit(1, title="title from the first ranking")
    from_second = _hit(1, title="title from the second ranking")

    # Act
    fused = reciprocal_rank_fusion([[from_first], [from_second]])

    # Assert
    assert len(fused) == 1
    assert fused[0].title == "title from the first ranking"
    assert fused[0].rank == pytest.approx(2 / 61, abs=1e-12)


def test_the_first_ranking_that_contains_the_document_supplies_its_fields() -> None:
    # Arrange
    only_in_second = _hit(1, title="found later")
    other = _hit(2)
    also_in_third = _hit(1, title="found even later")

    # Act
    fused = reciprocal_rank_fusion([[other], [only_in_second], [also_in_third]])

    # Assert
    by_id = {hit.ref_id: hit for hit in fused}
    assert by_id[_id(1)].title == "found later"
    assert by_id[_id(1)].rank == pytest.approx(2 / 61, abs=1e-12)


def test_the_fused_hit_keeps_kind_ref_id_title_body_and_occurred_at() -> None:
    # Arrange
    original = _hit(7, kind="incident", title="Settlement delayed", occurred_at=BASE_TIME)

    # Act
    fused = reciprocal_rank_fusion([[original]])

    # Assert
    assert fused == [
        SearchHit(
            kind="incident",
            ref_id=_id(7),
            title="Settlement delayed",
            body="body 7",
            occurred_at=BASE_TIME,
            rank=fused[0].rank,
        )
    ]
    assert fused[0].rank == pytest.approx(1 / 61, abs=1e-12)


@pytest.mark.parametrize(
    "rankings",
    [
        pytest.param([], id="no-rankings"),
        pytest.param([[]], id="one-empty-ranking"),
        pytest.param([[], []], id="two-empty-rankings"),
    ],
)
def test_empty_input_gives_an_empty_result(rankings: list[list[SearchHit]]) -> None:
    # Arrange: rankings is the parameter

    # Act
    fused = reciprocal_rank_fusion(rankings)

    # Assert
    assert fused == []


def test_an_empty_ranking_next_to_a_full_one_changes_nothing_but_the_scores_stay_single() -> None:
    # Arrange
    ranking = [_hit(1), _hit(2)]

    # Act
    fused = reciprocal_rank_fusion([[], ranking])

    # Assert
    assert [hit.ref_id for hit in fused] == [_id(1), _id(2)]
    assert [hit.rank for hit in fused] == pytest.approx([1 / 61, 1 / 62], abs=1e-12)


def test_fusion_does_not_modify_its_input_rankings() -> None:
    # Arrange
    first = [_hit(1, rank=0.9), _hit(2, rank=0.1)]
    second = [_hit(2, rank=0.8), _hit(1, rank=0.2)]
    first_before = list(first)
    second_before = list(second)

    # Act
    reciprocal_rank_fusion([first, second])

    # Assert
    assert first == first_before
    assert second == second_before
    assert [hit.rank for hit in first] == [0.9, 0.1]


def test_the_fused_scores_are_never_increasing() -> None:
    # Arrange
    first = [_hit(number) for number in range(1, 8)]
    second = [_hit(number) for number in (5, 3, 9, 1, 11)]

    # Act
    fused = reciprocal_rank_fusion([first, second], limit=20)

    # Assert
    scores = [hit.rank for hit in fused]
    assert scores == sorted(scores, reverse=True)
    assert len(fused) == 9
