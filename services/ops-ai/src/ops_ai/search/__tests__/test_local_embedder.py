import json
import math
import os
import subprocess
import sys

import pytest
from ops_ai.search.embedder import (
    LOCAL_DIMENSIONS,
    LOCAL_EMBEDDER_ID,
    Embedder,
    EmbedderUnavailableError,
    LocalHashEmbedder,
    cosine_similarity,
    document_text,
)

SELF_EMBED_SCRIPT = (
    "import json, sys; from ops_ai.search.embedder import LocalHashEmbedder; "
    "print(json.dumps(LocalHashEmbedder().embed([sys.argv[1]])[0]))"
)


def _embed(text: str) -> list[float]:
    return LocalHashEmbedder().embed([text])[0]


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def test_local_embedder_constants_and_attributes_match_the_contract() -> None:
    # Arrange
    embedder = LocalHashEmbedder()

    # Act
    attributes = (embedder.embedder_id, embedder.dimensions)

    # Assert
    assert (LOCAL_EMBEDDER_ID, LOCAL_DIMENSIONS) == ("local-hash-v1", 512)
    assert attributes == ("local-hash-v1", 512)


def test_local_embedder_is_assignable_to_the_embedder_port() -> None:
    # Arrange
    embedder: Embedder = LocalHashEmbedder()

    # Act
    vectors = embedder.embed(["card declined"])

    # Assert
    assert len(vectors) == 1
    assert len(vectors[0]) == embedder.dimensions


def test_embedder_unavailable_error_is_an_exception_that_keeps_its_message() -> None:
    # Arrange
    message = "provider outage"

    # Act
    with pytest.raises(EmbedderUnavailableError) as exc:
        raise EmbedderUnavailableError(message)

    # Assert
    assert type(exc.value) is EmbedderUnavailableError
    assert str(exc.value) == message


def test_embed_returns_one_512_dimension_vector_per_text_in_order() -> None:
    # Arrange
    texts = ["card declined", "settlement payout delayed", "insufficient funds"]

    # Act
    vectors = LocalHashEmbedder().embed(texts)

    # Assert
    assert [len(vector) for vector in vectors] == [512, 512, 512]
    assert vectors == [_embed(text) for text in texts]


def test_embed_returns_an_empty_list_for_no_texts() -> None:
    # Arrange
    texts: list[str] = []

    # Act
    vectors = LocalHashEmbedder().embed(texts)

    # Assert
    assert vectors == []


@pytest.mark.parametrize(
    "text", ["card declined", "Card payment declined: insufficient funds", "x", "BIN 400000"]
)
def test_embed_returns_a_unit_norm_vector_for_a_text_with_tokens(text: str) -> None:
    # Arrange
    embedder = LocalHashEmbedder()

    # Act
    vector = embedder.embed([text])[0]

    # Assert
    assert _norm(vector) == pytest.approx(1.0, abs=1e-9)


@pytest.mark.parametrize("text", ["!!!", "", "   ", "\n\t", "-- ## __ ..", "日本語"])
def test_embed_returns_the_zero_vector_for_a_text_without_tokens(text: str) -> None:
    # Arrange
    embedder = LocalHashEmbedder()

    # Act
    vector = embedder.embed([text])[0]

    # Assert
    assert vector == [0.0] * 512


def test_embed_returns_identical_vectors_on_repeat_and_across_instances() -> None:
    # Arrange
    text = "Card payment declined; decline code insufficient_funds"

    # Act
    first = LocalHashEmbedder().embed([text])[0]
    second = LocalHashEmbedder().embed([text])[0]
    third = LocalHashEmbedder().embed([text])[0]

    # Assert
    assert first == second == third


def test_embed_gives_the_same_vector_in_a_process_with_a_different_python_hash_seed() -> None:
    # Arrange
    text = "Card payment declined; decline code insufficient_funds"
    expected = _embed(text)

    def run(seed: str) -> list[float]:
        completed = subprocess.run(  # noqa: S603
            [sys.executable, "-c", SELF_EMBED_SCRIPT, text],
            capture_output=True,
            check=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        parsed: list[float] = json.loads(completed.stdout)
        return parsed

    # Act
    other_seeds = [run("1"), run("2")]

    # Assert
    assert other_seeds == [expected, expected]


def test_embed_of_a_batch_equals_embedding_each_text_alone() -> None:
    # Arrange
    texts = ["card declined", "insufficient funds"]

    # Act
    batch = LocalHashEmbedder().embed(texts)

    # Assert
    assert batch == [_embed("card declined"), _embed("insufficient funds")]


def test_embed_is_case_insensitive() -> None:
    # Arrange
    lower = "card payment declined"
    mixed = "Card PAYMENT Declined"

    # Act
    lower_vector = _embed(lower)
    mixed_vector = _embed(mixed)

    # Assert
    assert mixed_vector == lower_vector


@pytest.mark.parametrize("text", ["card-declined", "card_declined", "card, declined!", "  card   declined  "])
def test_embed_splits_tokens_on_anything_that_is_not_a_letter_or_digit(text: str) -> None:
    # Arrange
    reference = _embed("card declined")

    # Act
    vector = _embed(text)

    # Assert
    assert vector == reference


def test_embed_keeps_digits_as_token_characters() -> None:
    # Arrange
    with_bin = _embed("BIN 400000")
    other_bin = _embed("BIN 424242")

    # Act
    similarity = cosine_similarity(with_bin, other_bin)

    # Assert
    assert with_bin != other_bin
    assert similarity < 1.0


def test_embed_a_repeated_token_keeps_the_same_direction_as_the_single_token() -> None:
    # Arrange
    single = _embed("declined")

    # Act
    repeated = _embed("declined declined")

    # Assert
    assert cosine_similarity(single, repeated) == pytest.approx(1.0, abs=1e-9)


def test_embed_a_typo_is_closer_to_the_right_word_than_to_an_unrelated_word() -> None:
    # Arrange
    typo = _embed("insufficent")

    # Act
    to_right_word = cosine_similarity(typo, _embed("insufficient"))
    to_unrelated = cosine_similarity(typo, _embed("refrigerator"))

    # Assert
    assert to_right_word > 0.5
    assert to_unrelated < 0.1
    assert to_right_word > to_unrelated


def test_embed_distinct_texts_give_distinct_vectors() -> None:
    # Arrange
    first = _embed("card payment declined")
    second = _embed("settlement payout delayed")

    # Act
    same = first == second

    # Assert
    assert same is False


def test_cosine_similarity_of_a_vector_with_itself_is_one() -> None:
    # Arrange
    vector = _embed("card payment declined")

    # Act
    similarity = cosine_similarity(vector, vector)

    # Assert
    assert similarity == pytest.approx(1.0, abs=1e-9)


def test_cosine_similarity_of_orthogonal_vectors_is_zero() -> None:
    # Arrange
    a = [1.0, 0.0, 0.0]
    b = [0.0, 1.0, 0.0]

    # Act
    similarity = cosine_similarity(a, b)

    # Assert
    assert similarity == pytest.approx(0.0, abs=1e-12)


def test_cosine_similarity_of_opposite_vectors_is_minus_one() -> None:
    # Arrange
    a = [1.0, 2.0, 3.0]
    b = [-1.0, -2.0, -3.0]

    # Act
    similarity = cosine_similarity(a, b)

    # Assert
    assert similarity == pytest.approx(-1.0, abs=1e-12)


def test_cosine_similarity_ignores_vector_length() -> None:
    # Arrange
    short = [1.0, 0.0]
    long = [5.0, 0.0]

    # Act
    similarity = cosine_similarity(short, long)

    # Assert
    assert similarity == pytest.approx(1.0, abs=1e-12)


def test_cosine_similarity_matches_the_known_angle_between_45_degrees() -> None:
    # Arrange
    a = [1.0, 1.0]
    b = [1.0, 0.0]

    # Act
    similarity = cosine_similarity(a, b)

    # Assert
    assert similarity == pytest.approx(math.sqrt(2) / 2, abs=1e-12)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        pytest.param([0.0, 0.0], [1.0, 0.0], id="first-is-zero"),
        pytest.param([1.0, 0.0], [0.0, 0.0], id="second-is-zero"),
        pytest.param([0.0, 0.0], [0.0, 0.0], id="both-are-zero"),
    ],
)
def test_cosine_similarity_is_zero_when_either_vector_is_all_zero(a: list[float], b: list[float]) -> None:
    # Arrange: a and b are the parameters

    # Act
    similarity = cosine_similarity(a, b)

    # Assert
    assert similarity == 0.0


def test_cosine_similarity_with_the_embedding_of_a_text_without_tokens_is_zero() -> None:
    # Arrange
    empty = _embed("!!!")
    other = _embed("card declined")

    # Act
    similarity = cosine_similarity(empty, other)

    # Assert
    assert similarity == 0.0


def test_document_text_joins_title_and_body_with_one_space() -> None:
    # Arrange
    title = "Card payment declined"
    body = "decline code insufficient_funds"

    # Act
    text = document_text(title, body)

    # Assert
    assert text == "Card payment declined decline code insufficient_funds"
